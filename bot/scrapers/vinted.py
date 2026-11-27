from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional
from bs4 import BeautifulSoup

from bot.models import Offer
from bot.scrapers.base import BaseScraper, parse_price_number
from bot.services.filter import (
    classify_condition,
    contains_set_number,
    is_authentic_and_complete_set,
)
from bot.services.http_client import http_client

logger = logging.getLogger(__name__)


def apply_vinted_buyer_protection_fee(raw_price: float) -> float:
    """
    Compute Vinted Poland Buyer Protection fee-inclusive item price (`5% + 2.90 PLN`),
    e.g. 283.06 -> 300.11 PLN, when Vinted only returns the base item price.
    """
    return round(raw_price * 1.05 + 2.90, 2)


def _extract_vinted_amount_currency(raw_val, default_curr: str = "PLN") -> tuple[Optional[float], str]:
    curr = default_curr
    if isinstance(raw_val, dict):
        try:
            amt = float(raw_val.get("amount", 0))
            curr = str(raw_val.get("currency_code", curr) or curr)
            return (amt if amt > 0 else None), curr
        except (TypeError, ValueError):
            return None, curr
    if raw_val is not None:
        try:
            amt = float(str(raw_val).replace(",", "."))
            return (amt if amt > 0 else None), curr
        except ValueError:
            return None, curr
    return None, curr


class VintedScraper(BaseScraper):
    """
    Parser for vinted.pl using Vinted's SSR catalog pages (`price_low_to_high` & `newest_first`).
    - Directly fetches and parses SSR catalog HTML (`a[href*='/items/'][title]`) with zero
      edge-relay usage when direct access succeeds.
    - If blocked by WAF/Cloudflare on a datacenter IP (e.g. GitHub Actions Azure IP),
      falls back cleanly to a single throttled `r.jina.ai` catalog request per set.
    """

    source_name = "vinted.pl"

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
        full_scan: bool = False,
    ) -> list[Offer]:
        catalog_urls = [
            f"https://www.vinted.pl/catalog?search_text=lego%20{set_number}"
            f"&status_ids[]=6&status_ids[]=1&order=price_low_to_high",
            f"https://www.vinted.pl/catalog?search_text=lego%20{set_number}"
            f"&status_ids[]=6&status_ids[]=1&order=newest_first",
        ]

        offers: list[Offer] = []
        seen_ids: set[str] = set()
        any_direct_ok = False
        last_direct_status = 0

        for url in catalog_urls:
            self._record_url(url)
            status, html, _ = await http_client.get_text(
                url,
                use_jina_html_on_block=False,
                timeout=20.0,
            )
            if status in (429, 503):
                logger.info("Vinted direct returned HTTP %d; backing off 4s before retry", status)
                await asyncio.sleep(4.0)
                status, html, _ = await http_client.get_text(
                    url,
                    use_jina_html_on_block=False,
                    use_cache=False,
                    timeout=20.0,
                )
            last_direct_status = status
            if status == 200 and html and "vinted" in html.lower() and "cf-challenge" not in html.lower():
                any_direct_ok = True
                parsed = self._parse_vinted_catalog_html(html, set_number, msrp_pln, seen_ids)
                offers.extend(parsed)
            else:
                # If first direct fetch is WAF-blocked (403/429), skip second direct fetch and use edge fallback
                if not any_direct_ok:
                    break

        if any_direct_ok:
            self.last_error = None
            return offers

        # Datacenter IP fallback (GitHub Actions Azure IPs): single throttled edge-reader catalog fetch
        fb_ok, fb_status, fb_offers = await self._fetch_vinted_via_edge_catalog(set_number, msrp_pln)
        if fb_ok:
            self.last_error = None
            return fb_offers

        self._record_failure(
            f"HTTP {last_direct_status} direct / HTTP {fb_status} via r.jina.ai on {catalog_urls[0]}"
        )
        return []

    def _build_offer_from_catalog_meta(
        self,
        item_url: str,
        ext_id: str,
        meta_title: str,
        set_number: str,
        msrp_pln: Optional[float],
    ) -> Optional[Offer]:
        clean_url = item_url.split("?")[0]
        if not clean_url.startswith("http"):
            clean_url = f"https://www.vinted.pl{clean_url}"
        title_part = meta_title.split(", Marka:")[0].split(", Stan:")[0].strip()
        if not contains_set_number(title_part, set_number):
            return None

        # In Vinted catalog link titles ("..., 283.06 zł, 300.11 zł"), the last price includes the Vinted fee
        prices = re.findall(r"(\d[\d\s]*[,.]\d{2})\s*z[łl]", meta_title, flags=re.I)
        if not prices:
            return None
        if len(prices) >= 2:
            price_val = parse_price_number(prices[-1])
        else:
            raw_single = parse_price_number(prices[0])
            price_val = apply_vinted_buyer_protection_fee(raw_single) if raw_single else None
        if not price_val or price_val <= 0:
            return None

        if not is_authentic_and_complete_set(
            title=meta_title,
            set_number=set_number,
            msrp_pln=msrp_pln,
            item_price_pln=price_val,
        ):
            return None

        cond = classify_condition(title=title_part, raw_condition=meta_title)
        if not cond:
            return None

        return Offer(
            set_number=set_number,
            title=title_part[:120],
            source=self.source_name,
            store_name="Vinted.pl Seller",
            url=clean_url,
            external_id=ext_id,
            item_price_original=price_val,
            currency_original="PLN",
            item_price_pln=price_val,
            delivery_price_pln=None,
            delivery_time="InPost / Orlen Paczka (2-4 dni)",
            condition=cond,
        )

    def _parse_vinted_catalog_html(
        self,
        html: str,
        set_number: str,
        msrp_pln: Optional[float],
        seen_ids: set[str],
    ) -> list[Offer]:
        soup = BeautifulSoup(html, "lxml")
        offers: list[Offer] = []
        for a_tag in soup.select("a[href*='/items/'][title]"):
            href = str(a_tag.get("href", "")).strip()
            meta_title = str(a_tag.get("title", "")).strip()
            if not href or not meta_title:
                continue
            m = re.search(r"/items/(\d+)", href)
            if not m:
                continue
            ext_id = m.group(1)
            if ext_id in seen_ids:
                continue
            seen_ids.add(ext_id)
            offer = self._build_offer_from_catalog_meta(
                item_url=href,
                ext_id=ext_id,
                meta_title=meta_title,
                set_number=set_number,
                msrp_pln=msrp_pln,
            )
            if offer:
                offers.append(offer)
        return offers

    async def _fetch_vinted_via_edge_catalog(
        self,
        set_number: str,
        msrp_pln: Optional[float],
    ) -> tuple[bool, int, list[Offer]]:
        catalog_url = (
            f"https://www.vinted.pl/catalog?search_text=lego%20{set_number}"
            f"&status_ids[]=6&status_ids[]=1&order=price_low_to_high"
        )
        edge_url = f"https://r.jina.ai/{catalog_url}"
        self._record_url(catalog_url)
        status, md_text, _ = await http_client.get_text(
            edge_url,
            use_cf_worker_on_block=False,
            use_jina_html_on_block=False,
            timeout=22.0,
        )
        if status != 200 or not md_text:
            return False, status, []

        offers: list[Offer] = []
        pattern = re.compile(
            r'\[\]\((https://www\.vinted\.pl/items/(\d+)[^\s)"]*)\s+"([^"]+)"\)'
        )
        seen_ids: set[str] = set()
        for match in pattern.finditer(md_text):
            item_url, ext_id, meta_title = match.group(1), match.group(2), match.group(3)
            if ext_id in seen_ids:
                continue
            seen_ids.add(ext_id)
            offer = self._build_offer_from_catalog_meta(
                item_url=item_url,
                ext_id=ext_id,
                meta_title=meta_title,
                set_number=set_number,
                msrp_pln=msrp_pln,
            )
            if offer:
                offers.append(offer)
        return True, 200, offers
