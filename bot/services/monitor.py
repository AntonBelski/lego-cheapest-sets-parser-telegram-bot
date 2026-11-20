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
from bot.keyboards import main_menu_keyboard
from bot.models import (
    Offer,
    PriceStats,
    ScraperSiteReport,
    SetMetadata,
    Subscription,
    format_logged_url_with_browser_link,
    format_retirement_status_and_countdown,
    format_source_label,
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
from bot.services.chart import fetch_set_image_bytes, render_deal_card_with_chart
from bot.services.currency import currency_converter
from bot.services.filter import is_valid_offer_url
from bot.services.http_client import http_client
from bot.services.retirement import match_retirement_milestone, retirement_service

logger = logging.getLogger(__name__)


def classify_scraper_error(error_message: Optional[str]) -> str:
    """Group scraper error messages into human-readable error categories."""
    msg = (error_message or "").lower()
    if not msg:
        return "Other / Unknown Error"
    if "429" in msg or "rate limit" in msg or "too many requests" in msg:
        return "HTTP 429 / Rate Limit"
    if "403" in msg or "datadome" in msg or "waf" in msg or "forbidden" in msg:
        return "HTTP 403 / WAF Block"
    if re.search(r"\b50[0-9]\b", msg):
        return "HTTP 5xx / Upstream Server Error"
    if "http 0" in msg or "timeout" in msg or "timed out" in msg or "connection" in msg:
        return "Network / Timeout (HTTP 0)"
    if "failed fetching" in msg:
        return "Fetch / Relay Failure"
    return "Other / Parse Error"


def format_grouped_errors_summary(failed_reports: list[ScraperSiteReport]) -> str:
    """Format failed scraper reports grouped by error type and site."""
    if not failed_reports:
        return ""
    grouped: dict[str, dict[str, list[str]]] = {}
    for rep in failed_reports:
        err_type = classify_scraper_error(rep.error_message)
        grouped.setdefault(err_type, {}).setdefault(rep.source, []).append(rep.set_number)

    lines = [f"[!] ERRORS GROUPED BY TYPE ({len(failed_reports)} failure(s)):"]
    for err_type, by_site in sorted(
        grouped.items(),
        key=lambda kv: -sum(len(sets) for sets in kv[1].values()),
    ):
        type_total = sum(len(sets) for sets in by_site.values())
        lines.append(f"  • {err_type} ({type_total}):")
        for site, set_nums in sorted(by_site.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            unique_sets = sorted(set(set_nums))
            lines.append(
                f"      - {site}: {len(set_nums)} set(s) [{', '.join(unique_sets)}]"
            )
    return "\n".join(lines)


class LegoMonitorService:
    """
    Coordinates multi-site scraping, full price history persistence, smart deduplication,
    price-drop detection (previous_price -> new_price), detailed per-run URL logging,
    chart generation, and Telegram notifications.
    """

    def __init__(self, db: Database, bot: Optional[Bot] = None) -> None:
        self.db = db
        self.bot = bot
        self.last_run_failed_sites_count: int = 0
        self.last_run_parser_summary: str = ""
        self.promoklocki = PromoklockiScraper()
        self.scrapers: list[BaseScraper] = [
            OlxScraper(),
            VintedScraper(),
            AllegroLokalnieScraper(),
            AllegroScraper(),
            CeneoScraper(),
            # EbayScraper(),       # Temporarily disabled per user request
            # BricklinkScraper(),  # Temporarily disabled per user request
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
        # Also resolve retirement date & English set name from Brickset/BrickEconomy
        try:
            ret_info = await retirement_service.fetch_retirement_info(set_number)
            if ret_info:
                if meta is None:
                    meta = SetMetadata(set_number=set_number)
                if ret_info.retirement_date:
                    meta.retirement_date = ret_info.retirement_date
                    meta.retirement_source = ret_info.source
                    meta.is_retired = ret_info.is_already_retired
                if ret_info.set_name:
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

        # 1. Fetch Promoklocki first so we always have MSRP and image URL
        english_name: Optional[str] = None
        if any(s.set_name for s in subs_for_set):
            english_name = next((s.set_name for s in subs_for_set if s.set_name), None)

        self.promoklocki._reset_run_trace()
        try:
            meta, pk_offers, history_pts = await self.promoklocki.fetch_metadata_and_history(
                set_number
            )
            if meta:
                if meta.msrp_pln:
                    msrp_pln = meta.msrp_pln
                if english_name:
                    meta.name = english_name
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

    @staticmethod
    def format_per_set_parser_summary(
        by_set: dict[str, list[ScraperSiteReport]],
    ) -> str:
        """Format a compact per-set PASSED/FAILED summary across all parsers."""
        if not by_set:
            return ""
        summary_lines: list[str] = [
            "=" * 88,
            "📊 PER-SET PARSER SUMMARY",
            "=" * 88,
        ]
        for idx, (s_num, reps) in enumerate(by_set.items()):
            if idx > 0:
                summary_lines.append("")
            s_passed = sum(1 for r in reps if r.success)
            s_total = len(reps)
            summary_lines.append(f"Set {s_num} parsing:")
            summary_lines.append(f"Succeeded {s_passed}/{s_total} parsers:")
            for p_idx, r in enumerate(reps, 1):
                status_str = "PASSED" if r.success else "FAILED"
                summary_lines.append(f"{p_idx}) {r.source} - {status_str}")
        failed_all = [r for reps in by_set.values() for r in reps if not r.success]
        if failed_all:
            summary_lines.append("")
            summary_lines.append(format_grouped_errors_summary(failed_all))
        summary_lines.append("=" * 88)
        return "\n".join(summary_lines)

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
        self.last_run_failed_sites_count = len(failed_reports)
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
            total_sites = len(reps)
            for idx, r in enumerate(reps, start=1):
                badge = "SUCCESS" if r.success else "FAILED "
                err_tail = f" | Error: {r.error_message}" if r.error_message else ""
                iter_label = f"{idx}/{total_sites} {r.source}"
                lines.append(
                    f"  [{badge}] {iter_label:<25} | Offers: {len(r.offers):<3}{err_tail}"
                )
                for u in r.urls_scraped:
                    formatted_u = format_logged_url_with_browser_link(u, set_num)
                    lines.append(f"            ↳ URL: {formatted_u}")
                for off in r.offers:
                    lines.append(f"            • {off.format_log_item_line()}")

        if failed_reports:
            lines.append("")
            lines.append(format_grouped_errors_summary(failed_reports))
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
        lines.append(http_client.format_api_calls_summary())
        self.last_run_parser_summary = self.format_per_set_parser_summary(by_set)
        if self.last_run_parser_summary:
            lines.append(self.last_run_parser_summary)

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
        matching = [
            o
            for o in all_offers
            if sub.matches_price(o.total_price_pln) and is_valid_offer_url(o.url)
        ]
        if not matching:
            return

        unnotified: list[Offer] = []
        for offer in matching:
            if await self.db.should_notify_offer(sub.chat_id, offer):
                unnotified.append(offer)

        if not unnotified:
            return

        best_offer = unnotified[0]
        top_batch = unnotified[:20]
        caption, overflow_offers = self.split_caption_and_overflow_offers(
            sub, best_offer, top_batch, stats
        )

        try:
            img_bytes = await fetch_set_image_bytes(
                set_number=sub.set_number,
                preferred_url=sub.image_url or best_offer.image_url,
            )
            if self.bot:
                if img_bytes:
                    photo_file = BufferedInputFile(
                        img_bytes,
                        filename=f"lego_{sub.set_number}.jpg",
                    )
                    await self.bot.send_photo(
                        chat_id=sub.chat_id,
                        photo=photo_file,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=main_menu_keyboard(),
                    )
                else:
                    await self.bot.send_message(
                        chat_id=sub.chat_id,
                        text=caption,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=main_menu_keyboard(),
                    )
                # Send a second text-only message ONLY when offers didn't all fit inside the photo caption
                if overflow_offers:
                    batch_text = self.format_full_offers_batch_message(sub, top_batch)
                    await self.bot.send_message(
                        chat_id=sub.chat_id,
                        text=batch_text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=main_menu_keyboard(),
                    )
            # Mark ALL unnotified matching offers from this scan as notified so they are
            # never dripped out in small batches of 2-4 on subsequent 5-minute runs!
            for off in unnotified:
                await self.db.mark_offer_notified(sub.chat_id, off)
        except Exception as exc:
            logger.error(
                "Failed to send Telegram deal notification for %s: %s",
                sub.set_number,
                exc,
            )

    @staticmethod
    def _format_single_offer_html_line(idx: int, off: Offer) -> str:
        if off.delivery_is_estimated and off.delivery_price_pln:
            deliv_part = (
                f"{off.item_price_pln:.2f} PLN + "
                f"<b>{off.delivery_price_pln:.2f} PLN est. EU delivery</b>"
            )
        elif off.delivery_price_pln is None:
            deliv_part = "delivery unknown"
        elif off.delivery_price_pln == 0:
            deliv_part = "free delivery"
        else:
            deliv_part = f"{off.item_price_pln:.2f} PLN + {off.delivery_price_pln:.2f} PLN delivery"

        orig_part = (
            f" [{off.item_price_original:.2f} {off.currency_original.upper()}]"
            if off.currency_original.upper() != "PLN"
            else ""
        )
        safe_href = html.escape(sanitize_url_for_telegram(off.url), quote=True)
        return (
            f'{idx}. <a href="{safe_href}"><b>{html.escape(off.short_store_label)} - {off.total_price_pln:.2f} PLN</b></a> '
            f"({deliv_part}{html.escape(orig_part)})"
        )

    @classmethod
    def format_full_offers_batch_message(
        cls,
        sub: Subscription,
        offers: list[Offer],
    ) -> str:
        header = (
            f"🛒 <b>All {len(offers)} New / Price-Dropped Offers for LEGO {html.escape(sub.set_number)} "
            f"(up to 20 in one message):</b>\n"
        )
        output_lines = [header]
        total_len = len(header)
        for idx, off in enumerate(offers[:20], start=1):
            line = cls._format_single_offer_html_line(idx, off)
            if total_len + len(line) + 1 > 3950:
                break
            output_lines.append(line)
            total_len += len(line) + 1
        return "\n".join(output_lines)

    @classmethod
    def _build_base_caption_lines(
        cls,
        sub: Subscription,
        best: Offer,
    ) -> list[str]:
        raw_name = (sub.set_name or best.title or f"Set {sub.set_number}").strip()
        raw_name = re.sub(
            rf"^LEGO\s*®?\s*(?:Ideas\s+|Star Wars\s+|Technic\s+|Icons\s+|Creator\s+|City\s+|Marvel\s+|Harry Potter\s+|Ninjago\s+)?{re.escape(sub.set_number)}\s*[-–:]?\s*",
            "",
            raw_name,
            flags=re.I,
        ).strip() or raw_name
        set_title = html.escape(raw_name)

        msrp_str = f"{sub.msrp_pln:.2f} PLN" if (sub.msrp_pln and sub.msrp_pln > 0) else "N/A"

        detail_parts_html: list[str] = []
        if best.delivery_is_estimated and best.delivery_price_pln:
            detail_parts_html.append(
                f"{best.item_price_pln:.2f} PLN + <b>{best.delivery_price_pln:.2f} PLN estimated EU delivery</b>"
            )
        elif best.delivery_price_pln is None:
            detail_parts_html.append("delivery price unknown")
        elif best.delivery_price_pln == 0:
            detail_parts_html.append("free delivery")
        else:
            detail_parts_html.append(
                f"{best.item_price_pln:.2f} PLN + {best.delivery_price_pln:.2f} PLN delivery"
            )

        if sub.msrp_pln and sub.msrp_pln > 0:
            diff_pct = ((sub.msrp_pln - best.total_price_pln) / sub.msrp_pln) * 100
            if diff_pct > 0:
                detail_parts_html.append(f"-{diff_pct:.1f}% vs LEGO MSRP")
            else:
                detail_parts_html.append(f"+{abs(diff_pct):.1f}% vs LEGO MSRP")

        details_html = ", ".join(detail_parts_html)
        best_href = html.escape(sanitize_url_for_telegram(best.url), quote=True)
        store_link = f'<a href="{best_href}"><b>{html.escape(best.short_store_label)} - {best.total_price_pln:.2f} PLN</b></a>'

        ret_line = format_retirement_status_and_countdown(sub.retirement_date, None)

        return [
            f"<b>LEGO {html.escape(sub.set_number)} - {set_title}</b>",
            f"Official LEGO Catalog Price: <b>{msrp_str}</b>",
            f"Current Price: <b>{best.total_price_pln:.2f} PLN</b> ({details_html})",
            store_link,
            "",
            f"Your Price Range: <b>{html.escape(sub.formatted_range)}</b>",
            f"Retirement Status: {html.escape(ret_line)}",
        ]

    @classmethod
    def split_caption_and_overflow_offers(
        cls,
        sub: Subscription,
        best: Offer,
        top_new_offers: list[Offer],
        stats: PriceStats,
    ) -> tuple[str, list[Offer]]:
        base_lines = cls._build_base_caption_lines(sub, best)
        base_text = "\n".join(base_lines)

        if len(top_new_offers) <= 1:
            return base_text[:1000], []

        section_header = "\n\n🛒 <b>New / Price-Dropped Offers:</b>"
        offer_lines = [
            cls._format_single_offer_html_line(idx, off)
            for idx, off in enumerate(top_new_offers[:20], start=1)
        ]

        # Check if ALL offers fit inside Telegram's 1024-char photo caption
        full_candidate = base_text + section_header + "\n" + "\n".join(offer_lines)
        full_visible = re.sub(r"<[^>]+>", "", full_candidate)
        if len(full_visible) <= 1000 and len(full_candidate) <= 3800:
            return full_candidate, []

        # Otherwise fit as many lines as safely fit in caption and overflow the full batch to message #2
        fitted_caption = base_text
        for keep_count in range(len(offer_lines) - 1, 0, -1):
            cand = base_text + section_header + "\n" + "\n".join(offer_lines[:keep_count])
            vis = re.sub(r"<[^>]+>", "", cand)
            if len(vis) <= 1000 and len(cand) <= 3800:
                fitted_caption = cand
                break

        return fitted_caption, list(top_new_offers[:20])

    @classmethod
    def format_deal_notification_caption(
        cls,
        sub: Subscription,
        best: Offer,
        top_new_offers: list[Offer],
        stats: PriceStats,
    ) -> str:
        caption, _ = cls.split_caption_and_overflow_offers(sub, best, top_new_offers, stats)
        return caption

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
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=main_menu_keyboard(),
            )
        except Exception as exc:
            logger.error(
                "Failed to send retirement alert to %s: %s", chat_id, exc
            )
