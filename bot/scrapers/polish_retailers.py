from __future__ import annotations

import json
import logging
import re
from typing import Optional
from bs4 import BeautifulSoup

from bot.models import Offer
from bot.scrapers.base import BaseScraper, parse_price_number
from bot.services.filter import is_authentic_and_complete_set
from bot.services.http_client import http_client

logger = logging.getLogger(__name__)


def _extract_jsonld_products(soup: BeautifulSoup) -> list[dict]:
    """Extract Schema.org Product objects from <script type='application/ld+json'>."""
    products: list[dict] = []
    for script in soup.select("script[type='application/ld+json']"):
        raw = (script.string or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if not isinstance(item, dict):
                continue
            t = str(item.get("@type", "")).lower()
            if "product" in t:
                products.append(item)
            elif "@graph" in item and isinstance(item["@graph"], list):
                stack.extend(item["@graph"])
    return products


class PolishRetailersScraper(BaseScraper):
    """
    Direct parsers for major Polish LEGO retailers:
    - smyk.com
    - empik.com
    - mediaexpert.pl
    Ensures flash promotions at Poland's top 3 toy/electronics retailers are caught
    immediately even before price aggregators index them.
    """

    source_name = "smyk/empik/mediaexpert"

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
    ) -> list[Offer]:
        offers: list[Offer] = []
        for fn in (
            self._fetch_smyk,
            self._fetch_empik,
            self._fetch_mediaexpert,
        ):
            try:
                res = await fn(set_number, msrp_pln)
                offers.extend(res)
            except Exception as exc:
                logger.debug("Retailer parser error for %s: %s", set_number, exc)
        return offers

    async def _fetch_smyk(
        self,
        set_number: str,
        msrp_pln: Optional[float],
    ) -> list[Offer]:
        url = f"https://www.smyk.com/search?q=lego%20{set_number}"
        status, html, _ = await http_client.get_text(url)
        if status != 200 or not html:
            return []
        return self._parse_retail_html(
            html=html,
            set_number=set_number,
            msrp_pln=msrp_pln,
            source="smyk.com",
            store_name="Smyk.com",
            base_url="https://www.smyk.com",
            free_shipping_threshold=149.0,
            standard_shipping=12.99,
        )

    async def _fetch_empik(
        self,
        set_number: str,
        msrp_pln: Optional[float],
    ) -> list[Offer]:
        url = f"https://www.empik.com/szukaj/produkt?q=lego%20{set_number}"
        status, html, _ = await http_client.get_text(url)
        if status != 200 or not html:
            return []
        return self._parse_retail_html(
            html=html,
            set_number=set_number,
            msrp_pln=msrp_pln,
            source="empik.com",
            store_name="Empik.com",
            base_url="https://www.empik.com",
            free_shipping_threshold=100.0,
            standard_shipping=11.99,
        )

    async def _fetch_mediaexpert(
        self,
        set_number: str,
        msrp_pln: Optional[float],
    ) -> list[Offer]:
        url = f"https://www.mediaexpert.pl/search?query[menu_item]=&query[querystring]=lego%20{set_number}"
        status, html, _ = await http_client.get_text(url)
        if status != 200 or not html:
            return []
        return self._parse_retail_html(
            html=html,
            set_number=set_number,
            msrp_pln=msrp_pln,
            source="mediaexpert.pl",
            store_name="MediaExpert.pl",
            base_url="https://www.mediaexpert.pl",
            free_shipping_threshold=99.0,
            standard_shipping=10.99,
        )

    def _parse_retail_html(
        self,
        html: str,
        set_number: str,
        msrp_pln: Optional[float],
        source: str,
        store_name: str,
        base_url: str,
        free_shipping_threshold: float,
        standard_shipping: float,
    ) -> list[Offer]:
        soup = BeautifulSoup(html, "lxml")
        offers: list[Offer] = []

        # 1. Check JSON-LD Product schemas first
        for prod in _extract_jsonld_products(soup):
            name = str(prod.get("name", "")).strip()
            offer_obj = prod.get("offers", {})
            if isinstance(offer_obj, list) and offer_obj:
                offer_obj = offer_obj[0]
            if not isinstance(offer_obj, dict):
                continue

            avail = str(offer_obj.get("availability", "")).lower()
            if "outofstock" in avail:
                continue

            price_val = parse_price_number(str(offer_obj.get("price", "")))
            if not price_val:
                continue

            if not is_authentic_and_complete_set(
                title=name,
                set_number=set_number,
                msrp_pln=msrp_pln,
                item_price_pln=price_val,
            ):
                continue

            prod_url = str(offer_obj.get("url") or prod.get("url") or base_url).strip()
            if not prod_url.startswith("http"):
                prod_url = f"{base_url}{prod_url}"

            delivery_pln = 0.0 if price_val >= free_shipping_threshold else standard_shipping
            sku = str(prod.get("sku") or prod.get("mpn") or f"{source}-{set_number}")

            offers.append(
                Offer(
                    set_number=set_number,
                    title=name,
                    source=source,
                    store_name=store_name,
                    url=prod_url,
                    external_id=sku,
                    item_price_original=price_val,
                    currency_original="PLN",
                    item_price_pln=price_val,
                    delivery_price_pln=delivery_pln,
                    delivery_time="1-2 dni robocze",
                    condition="New (Sealed)",
                )
            )

        if offers:
            return offers

        # 2. Fallback HTML card selector
        cards = soup.select(
            ".search-list-item, .offer-box, .complex-product, [data-product-name], article"
        )
        for idx, card in enumerate(cards[:10]):
            text = card.get_text(" ", strip=True)
            if set_number not in text:
                continue
            price_m = re.search(r"(\d[\d\s]*[,.]\d{2})\s*z[łl]", text, flags=re.I)
            if not price_m:
                continue
            price_val = parse_price_number(price_m.group(1))
            if not price_val:
                continue

            a_tag = card.select_one("a[href]")
            if not a_tag:
                continue
            href = str(a_tag.get("href", "")).strip()
            full_url = href if href.startswith("http") else f"{base_url}{href}"
            title = a_tag.get_text(" ", strip=True) or text[:100]

            if not is_authentic_and_complete_set(
                title=title,
                set_number=set_number,
                msrp_pln=msrp_pln,
                item_price_pln=price_val,
            ):
                continue

            delivery_pln = 0.0 if price_val >= free_shipping_threshold else standard_shipping
            ext_id = re.sub(r"[^a-zA-Z0-9_-]", "", href[-24:]) or f"{source}-{idx}"

            offers.append(
                Offer(
                    set_number=set_number,
                    title=title,
                    source=source,
                    store_name=store_name,
                    url=full_url,
                    external_id=ext_id,
                    item_price_original=price_val,
                    currency_original="PLN",
                    item_price_pln=price_val,
                    delivery_price_pln=delivery_pln,
                    delivery_time="1-2 dni robocze",
                    condition="New (Sealed)",
                )
            )

        return offers
