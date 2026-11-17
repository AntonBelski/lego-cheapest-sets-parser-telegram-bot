from __future__ import annotations

import logging
import re
from typing import Optional

from bot.models import Offer
from bot.scrapers.base import BaseScraper, parse_price_number
from bot.services.currency import currency_converter
from bot.services.filter import (
    classify_condition,
    contains_set_number,
    is_authentic_and_complete_set,
)
from bot.services.http_client import HAS_CURL_CFFI, http_client

logger = logging.getLogger(__name__)


class VintedScraper(BaseScraper):
    """
    Parser for vinted.pl using Vinted's v2 catalog JSON endpoint.
    - On first scan (`full_scan=True`): Paginates `page=1..3` (`per_page=40`)
      plus `order=newest_first`.
    - On every 5-minute scan (`full_scan=False`): Checks BOTH `order=newest_first`
      AND `order=price_low_to_high` so both brand-new items and price reductions
      on existing items are captured immediately.
    """

    source_name = "vinted.pl"

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
        full_scan: bool = False,
    ) -> list[Offer]:
        urls_to_fetch: list[str] = []
        if full_scan:
            for page in (1, 2, 3):
                urls_to_fetch.append(
                    f"https://www.vinted.pl/api/v2/catalog/items"
                    f"?page={page}&per_page=40&search_text=lego%20{set_number}"
                    f"&status_ids[]=6&status_ids[]=1&order=price_low_to_high"
                )
            urls_to_fetch.append(
                f"https://www.vinted.pl/api/v2/catalog/items"
                f"?page=1&per_page=40&search_text=lego%20{set_number}"
                f"&status_ids[]=6&status_ids[]=1&order=newest_first"
            )
        else:
            urls_to_fetch = [
                f"https://www.vinted.pl/api/v2/catalog/items"
                f"?page=1&per_page=40&search_text=lego%20{set_number}"
                f"&status_ids[]=6&status_ids[]=1&order=newest_first",
                f"https://www.vinted.pl/api/v2/catalog/items"
                f"?page=1&per_page=40&search_text=lego%20{set_number}"
                f"&status_ids[]=6&status_ids[]=1&order=price_low_to_high",
            ]

        offers: list[Offer] = []
        any_success = False

        for search_url in urls_to_fetch:
            self._record_url(search_url)
            ok, items = await self._fetch_vinted_items(search_url)
            if not ok:
                self._record_failure(f"Failed fetching {search_url}")
                continue
            any_success = True

            for item in items:
                if not isinstance(item, dict):
                    continue

                ext_id = str(item.get("id", ""))
                title = str(item.get("title", "")).strip()
                url = str(item.get("url", "")).strip()
                if not url.startswith("http"):
                    url = f"https://www.vinted.pl{url}"

                if not contains_set_number(title, set_number):
                    continue

                raw_price = item.get("total_item_price") or item.get("price")
                currency = str(item.get("currency", "PLN") or "PLN")
                price_val: Optional[float] = None

                if isinstance(raw_price, dict):
                    try:
                        price_val = float(raw_price.get("amount", 0))
                        currency = str(raw_price.get("currency_code", currency))
                    except (TypeError, ValueError):
                        price_val = None
                elif raw_price is not None:
                    try:
                        price_val = float(str(raw_price).replace(",", "."))
                    except ValueError:
                        price_val = None

                if not price_val or price_val <= 0:
                    continue

                item_price_pln = await currency_converter.to_pln(price_val, currency)

                brand_title = str(item.get("brand_title", "")).strip()
                status_label = str(item.get("status", "")).strip()

                if not is_authentic_and_complete_set(
                    title=f"{brand_title} {title}",
                    set_number=set_number,
                    msrp_pln=msrp_pln,
                    item_price_pln=item_price_pln,
                ):
                    continue

                cond = classify_condition(
                    title=title,
                    raw_condition=status_label,
                )
                if not cond:
                    continue

                photo = item.get("photo", {})
                img_url = (
                    str(photo.get("url", "")) if isinstance(photo, dict) else None
                )

                offers.append(
                    Offer(
                        set_number=set_number,
                        title=title,
                        source=self.source_name,
                        store_name="Vinted.pl Seller",
                        url=url,
                        external_id=ext_id or url[-20:],
                        item_price_original=price_val,
                        currency_original=currency,
                        item_price_pln=item_price_pln,
                        delivery_price_pln=None,
                        delivery_time="InPost / Orlen Paczka (2-4 dni)",
                        condition=cond,
                        image_url=img_url,
                    )
                )

            if full_scan and len(items) < 35:
                break

        if any_success:
            self.last_error = None
            return offers

        # Datacenter IP fallback (GitHub Actions Azure IPs): parse Vinted catalog via edge reader
        fb_offers = await self._fetch_vinted_via_edge_catalog(set_number, msrp_pln)
        if fb_offers or self.last_error is None:
            self.last_error = None
        return fb_offers

    async def _fetch_vinted_via_edge_catalog(
        self,
        set_number: str,
        msrp_pln: Optional[float],
    ) -> list[Offer]:
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
            return []

        self.last_error = None
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
            clean_url = item_url.split("?")[0]
            title_part = meta_title.split(", Marka:")[0].split(", Stan:")[0].strip()
            if not contains_set_number(title_part, set_number):
                continue

            prices = re.findall(r"(\d[\d\s]*[,.]\d{2})\s*z[łl]", meta_title, flags=re.I)
            if not prices:
                continue
            price_val = parse_price_number(prices[0])
            if not price_val or price_val <= 0:
                continue

            if not is_authentic_and_complete_set(
                title=meta_title,
                set_number=set_number,
                msrp_pln=msrp_pln,
                item_price_pln=price_val,
            ):
                continue

            cond = classify_condition(title=title_part, raw_condition=meta_title)
            if not cond:
                continue

            offers.append(
                Offer(
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
            )
        return offers

    async def _fetch_vinted_items(self, api_url: str) -> tuple[bool, list[dict]]:
        """Acquire Vinted session cookie and query the JSON catalog endpoint."""
        if HAS_CURL_CFFI:
            from curl_cffi.requests import AsyncSession
            try:
                async with AsyncSession(impersonate="chrome124", timeout=18.0) as session:
                    await session.get("https://www.vinted.pl")
                    resp = await session.get(
                        api_url,
                        headers={
                            "Accept": "application/json, text/plain, */*",
                            "Accept-Language": "pl-PL,pl;q=0.9",
                            "Referer": "https://www.vinted.pl/",
                        },
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        items = data.get("items", []) if isinstance(data, dict) else []
                        return True, items
            except Exception as exc:
                logger.debug("Vinted session fetch failed: %s", exc)

        status, payload = await http_client.get_json(api_url)
        if status == 200 and isinstance(payload, dict):
            return True, payload.get("items", [])
        return False, []
