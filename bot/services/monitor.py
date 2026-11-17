from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime
from typing import Optional

from aiogram import Bot
from aiogram.types import BufferedInputFile

from bot.db import Database
from bot.models import Offer, PriceStats, SetMetadata, Subscription
from bot.scrapers.allegro import AllegroLokalnieScraper, AllegroScraper
from bot.scrapers.base import BaseScraper
from bot.scrapers.bricklink import BricklinkScraper
from bot.scrapers.brickmerge import BrickmergeScraper
from bot.scrapers.ceneo import CeneoScraper
from bot.scrapers.ebay import EbayScraper
from bot.scrapers.klocki_app import KlockiAppScraper
from bot.scrapers.olx import OlxScraper
from bot.scrapers.polish_retailers import PolishRetailersScraper
from bot.scrapers.promoklocki import PromoklockiScraper
from bot.scrapers.vinted import VintedScraper
from bot.services.chart import render_deal_card_with_chart
from bot.services.currency import currency_converter
from bot.services.retirement import match_retirement_milestone, retirement_service

logger = logging.getLogger(__name__)


class LegoMonitorService:
    """
    Coordinates multi-site scraping, full price history persistence, smart deduplication,
    chart generation, and Telegram notifications for both price deals and retirement dates.
    """

    def __init__(self, db: Database, bot: Optional[Bot] = None) -> None:
        self.db = db
        self.bot = bot
        self.promoklocki = PromoklockiScraper()
        self.scrapers: list[BaseScraper] = [
            OlxScraper(),
            VintedScraper(),
            AllegroLokalnieScraper(),
            AllegroScraper(),
            CeneoScraper(),
            EbayScraper(),
            BricklinkScraper(),
            BrickmergeScraper(),
            KlockiAppScraper(),
            PolishRetailersScraper(),
        ]

    async def enrich_set_on_subscribe(self, set_number: str) -> Optional[SetMetadata]:
        """
        Immediately fetch set name, LEGO.pl catalog price (MSRP), image, and historical
        daily lowest prices from Promoklocki when a user subscribes to a set.
        """
        await currency_converter.refresh_if_needed()
        meta, offers, history_points = await self.promoklocki.fetch_metadata_and_history(set_number)
        if meta:
            await self.db.update_set_metadata(meta)
        if history_points:
            await self.db.seed_historical_daily_prices(set_number, history_points)
        if offers:
            subs = await self.db.list_subscriptions()
            set_subs = [s for s in subs if s.set_number == set_number]
            await self.db.record_offers(set_number, offers, set_subs)
        return meta

    async def scan_single_set(self, set_number: str) -> list[Offer]:
        """
        Scan all 13 Polish & European marketplaces/retailers for `set_number`,
        store ALL found offers in the database (even outside target price range),
        and trigger notifications for any new offers matching subscribed users' ranges.
        """
        await currency_converter.refresh_if_needed()

        all_subs = await self.db.list_subscriptions()
        subs_for_set = [s for s in all_subs if s.set_number == set_number]
        msrp_pln: Optional[float] = next(
            (s.msrp_pln for s in subs_for_set if s.msrp_pln), None
        )

        # 1. Fetch Promoklocki first so we always have MSRP, set name, and image URL
        all_offers: list[Offer] = []
        try:
            meta, pk_offers, history_pts = await self.promoklocki.fetch_metadata_and_history(set_number)
            if meta:
                if meta.msrp_pln:
                    msrp_pln = meta.msrp_pln
                await self.db.update_set_metadata(meta)
            if history_pts:
                await self.db.seed_historical_daily_prices(set_number, history_pts)
            all_offers.extend(pk_offers)
            await self.db.record_scraper_health(
                self.promoklocki.source_name, "OK", len(pk_offers)
            )
        except Exception as exc:
            logger.warning("Promoklocki scrape error for %s: %s", set_number, exc)
            await self.db.record_scraper_health(
                self.promoklocki.source_name, "ERROR", 0, str(exc)
            )

        # 2. Run all other site scrapers concurrently (different domains run in parallel!)
        async def _run_scraper(scraper: BaseScraper) -> list[Offer]:
            try:
                res = await scraper.fetch_offers(set_number, msrp_pln=msrp_pln)
                await self.db.record_scraper_health(scraper.source_name, "OK", len(res))
                return res
            except Exception as exc:
                logger.warning("Scraper %s failed for %s: %s", scraper.source_name, set_number, exc)
                await self.db.record_scraper_health(
                    scraper.source_name, "ERROR", 0, str(exc)[:200]
                )
                return []

        results = await asyncio.gather(*(_run_scraper(s) for s in self.scrapers))
        for res_list in results:
            all_offers.extend(res_list)

        # Sort offers by total price with cheapest delivery ascending
        all_offers.sort(key=lambda o: o.total_price_pln)

        # 3. Record ALL offers in DB (even outside user price range)
        if all_offers:
            await self.db.record_offers(set_number, all_offers, subs_for_set)

        # Refresh subscriptions after metadata update
        all_subs = await self.db.list_subscriptions()
        subs_for_set = [s for s in all_subs if s.set_number == set_number]

        # 4. Evaluate user subscriptions & smart deduplication cache
        if self.bot and all_offers and subs_for_set:
            stats = await self.db.get_price_stats(set_number)
            for sub in subs_for_set:
                await self._evaluate_and_notify_subscriber(sub, all_offers, stats)

        return all_offers

    async def scan_all_subscribed_sets(self) -> dict[str, list[Offer]]:
        """Scan all unique LEGO sets currently subscribed by any user."""
        all_subs = await self.db.list_subscriptions()
        unique_sets = sorted({s.set_number for s in all_subs})
        results: dict[str, list[Offer]] = {}

        for set_num in unique_sets:
            offers = await self.scan_single_set(set_num)
            results[set_num] = offers

        return results

    async def _evaluate_and_notify_subscriber(
        self,
        sub: Subscription,
        all_offers: list[Offer],
        stats: PriceStats,
    ) -> None:
        # Filter offers within user's [price_from, price_to] range
        matching = [o for o in all_offers if sub.matches_price(o.total_price_pln)]
        if not matching:
            return

        # Filter out offers already notified at this or lower price
        unnotified: list[Offer] = []
        for offer in matching:
            if await self.db.should_notify_offer(sub.chat_id, offer):
                unnotified.append(offer)

        if not unnotified:
            return

        best_offer = unnotified[0]
        caption = self.format_deal_notification_caption(sub, best_offer, unnotified[:4], stats)

        try:
            chart_bytes = await render_deal_card_with_chart(
                subscription=sub,
                stats=stats,
                current_best_pln=best_offer.total_price_pln,
            )
            photo_file = BufferedInputFile(
                chart_bytes,
                filename=f"lego_{sub.set_number}_price_dynamics.png",
            )
            if self.bot:
                await self.bot.send_photo(
                    chat_id=sub.chat_id,
                    photo=photo_file,
                    caption=caption,
                    parse_mode="HTML",
                )
            for off in unnotified[:4]:
                await self.db.mark_offer_notified(sub.chat_id, off)
        except Exception as exc:
            logger.error("Failed to send Telegram deal notification for %s: %s", sub.set_number, exc)

    @staticmethod
    def format_deal_notification_caption(
        sub: Subscription,
        best: Offer,
        top_new_offers: list[Offer],
        stats: PriceStats,
    ) -> str:
        set_title = html.escape(sub.set_name or best.title or f"Set {sub.set_number}")
        msrp_line = "N/A"
        if sub.msrp_pln and sub.msrp_pln > 0:
            diff_pct = ((sub.msrp_pln - best.total_price_pln) / sub.msrp_pln) * 100
            disc_badge = f" (<b>-{diff_pct:.1f}%</b> vs LEGO.pl)" if diff_pct > 0 else f" (+{abs(diff_pct):.1f}% vs LEGO.pl)"
            msrp_line = f"<b>{sub.msrp_pln:.2f} PLN</b>{disc_badge}"

        low_30d_line = (
            f"<b>{stats.lowest_30d_pln:.2f} PLN</b> ({html.escape(stats.lowest_30d_source or '')}, {stats.lowest_30d_date})"
            if stats.lowest_30d_pln
            else f"<b>{best.total_price_pln:.2f} PLN</b> (today)"
        )
        low_all_line = (
            f"<b>{stats.lowest_all_time_pln:.2f} PLN</b> ({html.escape(stats.lowest_all_time_source or '')}, {stats.lowest_all_time_date})"
            if stats.lowest_all_time_pln
            else f"<b>{best.total_price_pln:.2f} PLN</b> (today)"
        )

        lines = [
            f"🔥 <b>NEW LOW PRICE IN YOUR RANGE: LEGO {html.escape(sub.set_number)}</b>",
            f"🧱 <b>{set_title}</b>",
            "",
            f"🏷 <b>LEGO.pl Catalog Price:</b> {msrp_line}",
            f"💰 <b>Current Best Price:</b> <b>{html.escape(best.formatted_price)}</b>",
            f"📦 <b>Condition:</b> {html.escape(best.condition)}",
            f"🚚 <b>Delivery Time:</b> {html.escape(best.delivery_time or 'Check seller page (+delivery)')}",
            f"🎯 <b>Your Price Range:</b> {html.escape(sub.formatted_range)}",
            "",
            f"📉 <b>Lowest (Last 30 Days):</b> {low_30d_line}",
            f"🏆 <b>Lowest (ALL TIME):</b> {low_all_line}",
        ]

        if sub.retirement_date:
            lines.append(f"⏳ <b>Retirement Date:</b> {html.escape(sub.retirement_date)} ({html.escape(sub.retirement_source or 'tracked')})")

        lines.append("")
        lines.append("🛒 <b>Buy Right Now (New Matching Links):</b>")
        for idx, off in enumerate(top_new_offers, start=1):
            deliv_note = f" | 🚚 {html.escape(off.delivery_time)}" if off.delivery_time else ""
            orig_note = (
                f" [{off.item_price_original:.2f} {off.currency_original}]"
                if off.currency_original.upper() != "PLN"
                else ""
            )
            lines.append(
                f"{idx}. <a href=\"{html.escape(off.url)}\"><b>{html.escape(off.store_name)}</b></a> — "
                f"<b>{html.escape(off.formatted_price)}</b>{orig_note} "
                f"(<i>{html.escape(off.condition)}</i>{deliv_note})"
            )

        return "\n".join(lines)[:1020]

    # -------------------------------------------------------------------------
    # Retirement Date Checking (Every ~5 days)
    # -------------------------------------------------------------------------
    async def check_all_retirement_dates(self, force_report_chat_id: Optional[int] = None) -> list[str]:
        """
        Check retirement dates for all subscribed sets across Brickset, BrickEconomy, and Promoklocki.
        Sends notifications:
        1) When a set receives a retirement date for the VERY FIRST TIME ('FIRST_DISCOVERED').
        2) Later ONLY on the exact milestones:
           1 year / 9m / 8m / 7m / 6m / 5m / 4m / 3m / 2m / 1m / 2 weeks.
        """
        all_subs = await self.db.list_subscriptions()
        unique_sets = sorted({s.set_number for s in all_subs})
        summaries: list[str] = []

        for set_num in unique_sets:
            info = await retirement_service.fetch_retirement_info(set_num)
            if not info or not info.retirement_date:
                summaries.append(f"• <b>LEGO {set_num}</b>: No retirement date announced yet.")
                continue

            ret_date_str = info.retirement_date.isoformat()
            days_left = (info.retirement_date - datetime.utcnow().date()).days

            # Save discovered retirement date to subscriptions metadata
            await self.db.update_set_metadata(
                SetMetadata(
                    set_number=set_num,
                    retirement_date=info.retirement_date,
                    retirement_source=info.source,
                    is_retired=info.is_already_retired,
                )
            )

            summaries.append(
                f"• <b>LEGO {set_num}</b>: Retires <b>{ret_date_str}</b> "
                f"({days_left} days left, source: {html.escape(info.source)})"
            )

            subs_for_set = [s for s in all_subs if s.set_number == set_num]
            for sub in subs_for_set:
                # 1. Check if this is the FIRST TIME a retirement date was discovered for this set
                first_sent = await self.db.has_sent_retirement_milestone(
                    sub.chat_id, set_num, "FIRST_DISCOVERED", ret_date_str
                )
                is_first_time = (sub.retirement_date != ret_date_str) and not first_sent

                if is_first_time:
                    msg = (
                        f"🚨 <b>NEW RETIREMENT DATE ANNOUNCED!</b>\n\n"
                        f"🧱 <b>LEGO {html.escape(set_num)} — {html.escape(sub.set_name or '')}</b>\n"
                        f"📅 <b>Retirement Date:</b> <b>{ret_date_str}</b> ({days_left} days remaining)\n"
                        f"🔎 <b>Source of Truth:</b> {html.escape(info.source)}\n\n"
                        f"<i>You will next be notified at milestones: 1 year, 9m, 8m, 7m, 6m, 5m, 4m, 3m, 2m, 1m, and 2 weeks before retirement.</i>"
                    )
                    await self._send_text_alert(sub.chat_id, msg)
                    await self.db.mark_retirement_milestone_sent(
                        sub.chat_id, set_num, "FIRST_DISCOVERED", ret_date_str
                    )

                # 2. Check if today matches one of the exact milestone countdown windows
                milestone = match_retirement_milestone(info.retirement_date)
                if milestone:
                    m_key, m_label, rem_days = milestone
                    already_sent = await self.db.has_sent_retirement_milestone(
                        sub.chat_id, set_num, m_key, ret_date_str
                    )
                    if not already_sent:
                        msg = (
                            f"⏳ <b>LEGO RETIREMENT COUNTDOWN: {m_label.upper()} LEFT!</b>\n\n"
                            f"🧱 <b>LEGO {html.escape(set_num)} — {html.escape(sub.set_name or '')}</b>\n"
                            f"⚠️ <b>Set will retire in {m_label}</b> ({rem_days} days left — <b>{ret_date_str}</b>)\n"
                            f"🔎 <b>Source:</b> {html.escape(info.source)}"
                        )
                        await self._send_text_alert(sub.chat_id, msg)
                        await self.db.mark_retirement_milestone_sent(
                            sub.chat_id, set_num, m_key, ret_date_str
                        )

        return summaries

    async def _send_text_alert(self, chat_id: int, text: str) -> None:
        if not self.bot:
            return
        try:
            await self.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception as exc:
            logger.error("Failed to send retirement alert to %s: %s", chat_id, exc)
