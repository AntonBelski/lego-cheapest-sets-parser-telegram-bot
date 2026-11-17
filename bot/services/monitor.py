from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.types import BufferedInputFile

from bot.db import Database
from bot.models import (
    Offer,
    PriceStats,
    ScraperSiteReport,
    SetMetadata,
    Subscription,
    format_retirement_status_and_countdown,
    sanitize_url_for_telegram,
)
from bot.scrapers.allegro import AllegroLokalnieScraper, AllegroScraper
from bot.scrapers.base import BaseScraper
from bot.scrapers.bricklink import BricklinkScraper
from bot.scrapers.brickmerge import BrickmergeScraper
from bot.scrapers.ceneo import CeneoScraper
from bot.scrapers.ebay import EbayScraper
from bot.scrapers.klocki_app import KlockiAppScraper
from bot.scrapers.olx import OlxScraper
from bot.scrapers.polish_retailers import (
    EmpikScraper,
    MediaExpertScraper,
    SmykScraper,
)
from bot.scrapers.promoklocki import PromoklockiScraper
from bot.scrapers.vinted import VintedScraper
from bot.services.chart import render_deal_card_with_chart
from bot.services.currency import currency_converter
from bot.services.retirement import match_retirement_milestone, retirement_service

logger = logging.getLogger(__name__)


class LegoMonitorService:
    """
    Coordinates multi-site scraping, full price history persistence, smart deduplication,
    price-drop detection (previous_price -> new_price), detailed per-run URL logging,
    chart generation, and Telegram notifications.
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
            KlockiAppScraper(),  # zklockow.pl
            SmykScraper(),
            EmpikScraper(),
            MediaExpertScraper(),
        ]

    async def enrich_set_on_subscribe(self, set_number: str) -> Optional[SetMetadata]:
        """
        Immediately fetch set name, LEGO.pl catalog price (MSRP), image, historical
        daily lowest prices, and retirement date when a user subscribes to a set.
        """
        await currency_converter.refresh_if_needed()
        meta, offers, history_points = await self.promoklocki.fetch_metadata_and_history(
            set_number
        )
        # Also resolve retirement date & fallback metadata from Brickset/BrickEconomy
        try:
            ret_info = await retirement_service.fetch_retirement_info(set_number)
            if ret_info:
                if meta is None:
                    meta = SetMetadata(set_number=set_number)
                if ret_info.retirement_date:
                    meta.retirement_date = ret_info.retirement_date
                    meta.retirement_source = ret_info.source
                    meta.is_retired = ret_info.is_already_retired
                if not meta.name and ret_info.set_name:
                    meta.name = ret_info.set_name
                if not meta.image_url and ret_info.image_url:
                    meta.image_url = ret_info.image_url
                if not meta.msrp_pln and ret_info.msrp_eur:
                    meta.msrp_pln = await currency_converter.to_pln(ret_info.msrp_eur, "EUR")
        except Exception as exc:
            logger.debug("Initial retirement lookup error for %s: %s", set_number, exc)

        if meta:
            await self.db.update_set_metadata(meta)
        if history_points:
            await self.db.seed_historical_daily_prices(set_number, history_points)
        if offers:
            subs = await self.db.list_subscriptions()
            set_subs = [s for s in subs if s.set_number == set_number]
            await self.db.record_offers(set_number, offers, set_subs)
        return meta

    async def scan_single_set_with_reports(
        self,
        set_number: str,
        force_full_scan: Optional[bool] = None,
    ) -> tuple[list[Offer], list[ScraperSiteReport]]:
        """
        Scan all 13 Polish & European marketplaces/retailers for `set_number`:
        - Uses `full_scan=True` (multi-page pagination) the first time a set is scanned,
          and `full_scan=False` (newest + lowest price dual check) on 5-minute runs.
        - Stores ALL found offers in the database (even outside target price range),
          populating `offer.previous_price_pln` when a listing's price changes.
        - Triggers notifications for any new or price-dropped offers within user's range.
        - Returns `(all_offers, site_reports)` for detailed run logging.
        """
        await currency_converter.refresh_if_needed()

        if force_full_scan is not None:
            full_scan = force_full_scan
        else:
            full_scan = await self.db.needs_initial_deep_scan(set_number)

        all_subs = await self.db.list_subscriptions()
        subs_for_set = [s for s in all_subs if s.set_number == set_number]
        msrp_pln: Optional[float] = next(
            (s.msrp_pln for s in subs_for_set if s.msrp_pln), None
        )

        # Ensure set name, fallback MSRP, image, and retirement date are populated if missing
        needs_meta = any(
            not s.retirement_date or not s.set_name or not s.msrp_pln
            for s in subs_for_set
        )
        if needs_meta:
            try:
                ret_info = await retirement_service.fetch_retirement_info(set_number)
                if ret_info:
                    est_msrp = (
                        await currency_converter.to_pln(ret_info.msrp_eur, "EUR")
                        if ret_info.msrp_eur
                        else None
                    )
                    if not msrp_pln and est_msrp:
                        msrp_pln = est_msrp
                    await self.db.update_set_metadata(
                        SetMetadata(
                            set_number=set_number,
                            name=ret_info.set_name or "",
                            msrp_pln=est_msrp,
                            image_url=ret_info.image_url,
                            retirement_date=ret_info.retirement_date,
                            retirement_source=ret_info.source,
                            is_retired=ret_info.is_already_retired,
                        )
                    )
            except Exception as exc:
                logger.debug("Retirement/metadata lookup during scan failed for %s: %s", set_number, exc)

        site_reports: list[ScraperSiteReport] = []
        all_offers: list[Offer] = []

        # 1. Fetch Promoklocki first so we always have MSRP, set name, and image URL
        self.promoklocki._reset_run_trace()
        try:
            meta, pk_offers, history_pts = await self.promoklocki.fetch_metadata_and_history(
                set_number
            )
            if meta:
                if meta.msrp_pln:
                    msrp_pln = meta.msrp_pln
                await self.db.update_set_metadata(meta)
            if history_pts:
                await self.db.seed_historical_daily_prices(set_number, history_pts)
            all_offers.extend(pk_offers)
            pk_success = self.promoklocki.last_error is None
            pk_report = ScraperSiteReport(
                set_number=set_number,
                source=self.promoklocki.source_name,
                urls_scraped=list(self.promoklocki.last_urls_scraped),
                success=pk_success,
                offers=pk_offers,
                error_message=self.promoklocki.last_error,
            )
            site_reports.append(pk_report)
            await self.db.record_scraper_health(
                self.promoklocki.source_name,
                "OK" if pk_success else "ERROR",
                len(pk_offers),
                urls_scraped=pk_report.urls_scraped,
                error_message=pk_report.error_message,
            )
        except Exception as exc:
            err_str = str(exc)[:200]
            pk_report = ScraperSiteReport(
                set_number=set_number,
                source=self.promoklocki.source_name,
                urls_scraped=list(self.promoklocki.last_urls_scraped),
                success=False,
                offers=[],
                error_message=err_str,
            )
            site_reports.append(pk_report)
            await self.db.record_scraper_health(
                self.promoklocki.source_name,
                "ERROR",
                0,
                urls_scraped=pk_report.urls_scraped,
                error_message=err_str,
            )

        # 2. Run all other site scrapers concurrently
        async def _run_scraper(scraper: BaseScraper) -> ScraperSiteReport:
            rep = await scraper.scrape_with_report(
                set_number=set_number,
                msrp_pln=msrp_pln,
                full_scan=full_scan,
            )
            await self.db.record_scraper_health(
                scraper.source_name,
                "OK" if rep.success else "ERROR",
                len(rep.offers),
                urls_scraped=rep.urls_scraped,
                error_message=rep.error_message,
            )
            return rep

        other_reports = await asyncio.gather(*(_run_scraper(s) for s in self.scrapers))
        for rep in other_reports:
            site_reports.append(rep)
            all_offers.extend(rep.offers)

        # Sort offers by total price with cheapest delivery ascending
        all_offers.sort(key=lambda o: o.total_price_pln)

        # 3. Record ALL offers in DB (even outside user price range) & populate `previous_price_pln`
        if all_offers:
            await self.db.record_offers(set_number, all_offers, subs_for_set)

        if full_scan:
            await self.db.mark_initial_scan_done(set_number)

        # Refresh subscriptions after metadata update
        all_subs = await self.db.list_subscriptions()
        subs_for_set = [s for s in all_subs if s.set_number == set_number]

        # 4. Evaluate user subscriptions & smart deduplication / price-drop cache
        if self.bot and all_offers and subs_for_set:
            stats = await self.db.get_price_stats(set_number)
            for sub in subs_for_set:
                await self._evaluate_and_notify_subscriber(sub, all_offers, stats)

        return all_offers, site_reports

    async def scan_single_set(self, set_number: str) -> list[Offer]:
        started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        offers, reports = await self.scan_single_set_with_reports(set_number)
        finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        await self._finalize_and_persist_run_logs(
            started_at=started_at,
            finished_at=finished_at,
            sets_scanned=1,
            all_reports=reports,
        )
        return offers

    async def scan_all_subscribed_sets(self) -> dict[str, list[Offer]]:
        """
        Scan all unique LEGO sets currently subscribed by any user,
        and output + persist comprehensive per-website URL & status logs.
        """
        started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        all_subs = await self.db.list_subscriptions()
        unique_sets = sorted({s.set_number for s in all_subs})
        results: dict[str, list[Offer]] = {}
        all_reports: list[ScraperSiteReport] = []

        for set_num in unique_sets:
            offers, reports = await self.scan_single_set_with_reports(set_num)
            results[set_num] = offers
            all_reports.extend(reports)

        finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        await self._finalize_and_persist_run_logs(
            started_at=started_at,
            finished_at=finished_at,
            sets_scanned=len(unique_sets),
            all_reports=all_reports,
        )
        return results

    async def _finalize_and_persist_run_logs(
        self,
        started_at: str,
        finished_at: str,
        sets_scanned: int,
        all_reports: list[ScraperSiteReport],
    ) -> None:
        """
        Format a scrollable run report showing every set, every website, every URL scraped,
        whether each website succeeded or failed, and mark the overall 5-minute run as
        FAILED if at least one website failed (while keeping all valid data in DB).
        """
        failed_reports = [r for r in all_reports if not r.success]
        total_offers = sum(len(r.offers) for r in all_reports)
        overall_status = (
            "SUCCESS"
            if len(failed_reports) == 0
            else f"FAILED ({len(failed_reports)}/{len(all_reports)} site checks failed — valid data saved)"
        )

        lines = [
            "=" * 88,
            f"5-MINUTE SCAN RUN REPORT | {started_at} -> {finished_at}",
            f"OVERALL RUN STATUS : {overall_status}",
            f"SETS SCANNED       : {sets_scanned} | TOTAL OFFERS SAVED: {total_offers} | FAILED SITES: {len(failed_reports)}",
            "=" * 88,
        ]

        by_set: dict[str, list[ScraperSiteReport]] = {}
        for rep in all_reports:
            by_set.setdefault(rep.set_number, []).append(rep)

        for set_num, reps in by_set.items():
            lines.append(f"\n--- LEGO SET {set_num} ---")
            for r in reps:
                badge = "SUCCESS" if r.success else "FAILED "
                err_tail = f" | Error: {r.error_message}" if r.error_message else ""
                lines.append(
                    f"  [{badge}] {r.source:<20} | Offers: {len(r.offers):<3}{err_tail}"
                )
                for u in r.urls_scraped:
                    lines.append(f"            ↳ URL: {u}")

        if failed_reports:
            lines.append(
                "\n[!] 5-MINUTE RUN MARKED AS FAILED BECAUSE AT LEAST ONE WEBSITE FAILED:"
            )
            for fr in failed_reports:
                lines.append(
                    f"    - Set {fr.set_number} @ {fr.source}: {fr.error_message or 'Failed'}"
                )
            lines.append(
                "    (Note: All offers from succeeded websites were preserved in the database.)"
            )
        lines.append("=" * 88)

        summary_text = "\n".join(lines)
        # 1. Print to stdout so GitHub Actions logs show the full scrollable table
        print(summary_text, flush=True)

        # 2. Append to persistent log file `data/scraper_runs.log`
        try:
            log_file = Path(self.db.db_path).parent / "scraper_runs.log"
            with log_file.open("a", encoding="utf-8") as f:
                f.write(summary_text + "\n\n")
        except Exception as exc:
            logger.debug("Failed writing scraper_runs.log: %s", exc)

        # 3. Persist to SQLite (`scan_runs` and `scan_run_site_logs`)
        await self.db.record_scan_run_logs(
            started_at=started_at,
            finished_at=finished_at,
            overall_status=overall_status,
            sets_scanned=sets_scanned,
            total_offers_found=total_offers,
            failed_sites_count=len(failed_reports),
            summary_log=summary_text,
            site_reports=all_reports,
        )

    async def _evaluate_and_notify_subscriber(
        self,
        sub: Subscription,
        all_offers: list[Offer],
        stats: PriceStats,
    ) -> None:
        matching = [o for o in all_offers if sub.matches_price(o.total_price_pln)]
        if not matching:
            return

        unnotified: list[Offer] = []
        for offer in matching:
            if await self.db.should_notify_offer(sub.chat_id, offer):
                unnotified.append(offer)

        if not unnotified:
            return

        best_offer = unnotified[0]
        caption = self.format_deal_notification_caption(
            sub, best_offer, unnotified[:4], stats
        )

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
            logger.error(
                "Failed to send Telegram deal notification for %s: %s",
                sub.set_number,
                exc,
            )

    @staticmethod
    def format_deal_notification_caption(
        sub: Subscription,
        best: Offer,
        top_new_offers: list[Offer],
        stats: PriceStats,
    ) -> str:
        set_title = html.escape(
            sub.set_name or best.title or f"Set {sub.set_number}"
        )
        msrp_line = "N/A"
        if sub.msrp_pln and sub.msrp_pln > 0:
            diff_pct = (
                (sub.msrp_pln - best.total_price_pln) / sub.msrp_pln
            ) * 100
            disc_badge = (
                f" (<b>-{diff_pct:.1f}%</b> vs LEGO.pl)"
                if diff_pct > 0
                else f" (+{abs(diff_pct):.1f}% vs LEGO.pl)"
            )
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

        ret_line = format_retirement_status_and_countdown(
            sub.retirement_date, sub.retirement_source
        )

        header_title = (
            f"📉 <b>PRICE DROP IN YOUR RANGE: LEGO {html.escape(sub.set_number)}</b>"
            if best.previous_price_pln is not None
            else f"🔥 <b>NEW LOW PRICE IN YOUR RANGE: LEGO {html.escape(sub.set_number)}</b>"
        )

        best_href = html.escape(sanitize_url_for_telegram(best.url), quote=True)
        lines = [
            header_title,
            f"🧱 <b>{set_title}</b>",
            "",
            f"🏷 <b>LEGO.pl Catalog Price:</b> {msrp_line}",
            f"💰 <b>Current Best Price:</b> <a href=\"{best_href}\"><b>{html.escape(best.formatted_price)} ↗</b></a>",
        ]

        if (
            best.previous_price_pln is not None
            and abs(best.previous_price_pln - best.total_price_pln) >= 1.0
        ):
            delta = best.previous_price_pln - best.total_price_pln
            lines.append(
                f"🔄 <b>Price Change:</b> <s>{best.previous_price_pln:.2f} PLN</s> ➔ "
                f"<b>{best.total_price_pln:.2f} PLN</b> (<b>-{delta:.2f} PLN</b>)"
            )

        lines.extend(
            [
                f"📦 <b>Condition:</b> {html.escape(best.condition)}",
                f"🚚 <b>Delivery Time:</b> {html.escape(best.delivery_time or 'Check seller page (+delivery)')}",
                f"🎯 <b>Your Price Range:</b> {html.escape(sub.formatted_range)}",
                "",
                f"📉 <b>Lowest (Last 30 Days):</b> {low_30d_line}",
                f"🏆 <b>Lowest (ALL TIME):</b> {low_all_line}",
                f"⏳ <b>Retirement Status:</b> {html.escape(ret_line)}",
                "",
                "🛒 <b>Buy Right Now (New / Price-Dropped Links):</b>",
            ]
        )

        offer_lines: list[str] = []
        for idx, off in enumerate(top_new_offers, start=1):
            deliv_note = (
                f" | 🚚 {html.escape(off.delivery_time[:35])}"
                if off.delivery_time
                else ""
            )
            orig_note = (
                f" [{off.item_price_original:.2f} {off.currency_original}]"
                if off.currency_original.upper() != "PLN"
                else ""
            )
            safe_href = html.escape(sanitize_url_for_telegram(off.url), quote=True)
            offer_lines.append(
                f"{idx}. <a href=\"{safe_href}\"><b>{html.escape(off.store_name[:42])}</b></a> — "
                f"<a href=\"{safe_href}\"><b>{html.escape(off.formatted_price)} ↗</b></a>{orig_note} "
                f"(<i>{html.escape(off.condition)}</i>{deliv_note})"
            )

        # Safely fit caption within Telegram's 1024-char limit without ever slicing inside an HTML tag
        base_text = "\n".join(lines)
        for keep_count in range(len(offer_lines), 0, -1):
            candidate = base_text + "\n" + "\n".join(offer_lines[:keep_count])
            visible_text = re.sub(r"<[^>]+>", "", candidate)
            if len(visible_text) <= 1000 and len(candidate) <= 1800:
                return candidate

        return base_text[:980]

    # -------------------------------------------------------------------------
    # Retirement Date Checking (Every ~5 days)
    # -------------------------------------------------------------------------
    async def check_all_retirement_dates(
        self, force_report_chat_id: Optional[int] = None
    ) -> list[str]:
        all_subs = await self.db.list_subscriptions()
        unique_sets = sorted({s.set_number for s in all_subs})
        summaries: list[str] = []

        for set_num in unique_sets:
            info = await retirement_service.fetch_retirement_info(set_num)
            if not info or not info.retirement_date:
                summaries.append(
                    f"• <b>LEGO {set_num}</b>: 🟢 Active (Not retired | No retirement date announced yet)"
                )
                continue

            ret_date_str = info.retirement_date.isoformat()
            await self.db.update_set_metadata(
                SetMetadata(
                    set_number=set_num,
                    retirement_date=info.retirement_date,
                    retirement_source=info.source,
                    is_retired=info.is_already_retired,
                )
            )

            status_str = format_retirement_status_and_countdown(
                ret_date_str, info.source
            )
            summaries.append(f"• <b>LEGO {set_num}</b>: {html.escape(status_str)}")

            subs_for_set = [s for s in all_subs if s.set_number == set_num]
            for sub in subs_for_set:
                first_sent = await self.db.has_sent_retirement_milestone(
                    sub.chat_id, set_num, "FIRST_DISCOVERED", ret_date_str
                )
                is_first_time = (
                    sub.retirement_date != ret_date_str
                ) and not first_sent

                if is_first_time:
                    msg = (
                        f"🚨 <b>NEW RETIREMENT DATE ANNOUNCED!</b>\n\n"
                        f"🧱 <b>LEGO {html.escape(set_num)} — {html.escape(sub.set_name or '')}</b>\n"
                        f"⏳ <b>{html.escape(status_str)}</b>\n\n"
                        f"<i>You will next be notified at milestones: 1 year, 9m, 8m, 7m, 6m, 5m, 4m, 3m, 2m, 1m, and 2 weeks before retirement.</i>"
                    )
                    await self._send_text_alert(sub.chat_id, msg)
                    await self.db.mark_retirement_milestone_sent(
                        sub.chat_id, set_num, "FIRST_DISCOVERED", ret_date_str
                    )

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
                            f"🔎 <b>Status:</b> {html.escape(status_str)}"
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
            await self.bot.send_message(
                chat_id=chat_id, text=text, parse_mode="HTML"
            )
        except Exception as exc:
            logger.error(
                "Failed to send retirement alert to %s: %s", chat_id, exc
            )
