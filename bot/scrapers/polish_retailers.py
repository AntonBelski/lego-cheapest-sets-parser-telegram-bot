from __future__ import annotations

import json
import logging
import re
from typing import Optional
from bs4 import BeautifulSoup

from bot.models import Offer
from bot.scrapers.base import BaseScraper, parse_price_number
from bot.services.filter import contains_set_number, is_authentic_and_complete_set
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


class _BasePolishRetailerScraper(BaseScraper):
    store_label: str = ""
    base_url: str = ""
    free_shipping_threshold: float = 100.0
    standard_shipping: float = 11.99

    def _parse_retail_html(
        self,
        html: str,
        set_number: str,
        msrp_pln: Optional[float],
    ) -> list[Offer]:
        soup = BeautifulSoup(html, "lxml")
        offers: list[Offer] = []

        # 1. Check JSON-LD Product schemas first (strictly verifying set_number in product name!)
        for prod in _extract_jsonld_products(soup):
            name = str(prod.get("name", "")).strip()
            # Strict check: the product's own title MUST contain the exact set_number!
            if not contains_set_number(name, set_number):
                continue

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

            prod_url = str(
                offer_obj.get("url") or prod.get("url") or self.base_url
            ).strip()
            if not prod_url.startswith("http"):
                prod_url = f"{self.base_url}{prod_url}"

            delivery_pln = (
                0.0
                if price_val >= self.free_shipping_threshold
                else self.standard_shipping
            )
            sku = str(
                prod.get("sku")
                or prod.get("mpn")
                or f"{self.source_name}-{set_number}"
            )

            offers.append(
                Offer(
                    set_number=set_number,
                    title=name,
                    source=self.source_name,
                    store_name=self.store_label,
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

        # 2. Fallback HTML product card selector (strictly verifying set_number in BOTH product title & link!)
        cards = soup.select(
            ".search-list-item, .offer-box, .complex-product, [data-product-name], article"
        )
        for idx, card in enumerate(cards[:15]):
            a_tag = card.select_one(
                "h2 a[href], h3 a[href], a.prod-name[href], a[title][href], a[href]"
            )
            if not a_tag:
                continue
            href = str(a_tag.get("href", "")).strip()
            title = (
                str(card.get("data-product-name") or "")
                or str(a_tag.get("title") or "")
                or a_tag.get_text(" ", strip=True)
            ).strip()

            # STRICT GUARD against Smyk / MediaExpert garbage search results:
            # The product card's own title AND/OR product URL slug MUST contain the exact set number!
            if not contains_set_number(title, set_number) and not contains_set_number(
                href, set_number
            ):
                continue

            text = card.get_text(" ", strip=True)
            # Extract price from dedicated main price elements first (avoiding installment/delivery numbers like "15,90 zł x 110 rat")
            price_val: Optional[float] = None
            for p_sel in (
                "[data-price]",
                ".main-price",
                ".price-box .price",
                ".product-price",
                ".price",
            ):
                p_el = card.select_one(p_sel)
                if p_el:
                    p_txt = str(p_el.get("data-price") or p_el.get_text(" ", strip=True))
                    if any(bad in p_txt.lower() for bad in ("rat", "miesięcz", "mies.", "dostaw")):
                        continue
                    candidate = parse_price_number(p_txt)
                    if candidate:
                        price_val = candidate
                        break

            if price_val is None:
                # Strip installment phrases like '15,90 zł x 60 rat' or 'rata od 15,90 zł' before regex matching
                cleaned_text = re.sub(
                    r"(?:rata(?:\s+od)?|raty(?:\s+od)?|\d+\s*x)\s*\d[\d\s]*[,.]\d{2}\s*z[łl]",
                    " ",
                    text,
                    flags=re.I,
                )
                cleaned_text = re.sub(
                    r"\d[\d\s]*[,.]\d{2}\s*z[łl]\s*(?:x\s*\d+|/\s*mies|miesięcz|rat)",
                    " ",
                    cleaned_text,
                    flags=re.I,
                )
                price_m = re.search(r"(\d[\d\s]*[,.]\d{2})\s*z[łl]", cleaned_text, flags=re.I)
                if not price_m:
                    continue
                price_val = parse_price_number(price_m.group(1))
            if not price_val:
                continue

            full_url = href if href.startswith("http") else f"{self.base_url}{href}"
            if not title:
                title = f"LEGO {set_number}"

            if not is_authentic_and_complete_set(
                title=title,
                set_number=set_number,
                msrp_pln=msrp_pln,
                item_price_pln=price_val,
            ):
                continue

            delivery_pln = (
                0.0
                if price_val >= self.free_shipping_threshold
                else self.standard_shipping
            )
            ext_id = (
                re.sub(r"[^a-zA-Z0-9_-]", "", href[-24:])
                or f"{self.source_name}-{idx}"
            )

            offers.append(
                Offer(
                    set_number=set_number,
                    title=title,
                    source=self.source_name,
                    store_name=self.store_label,
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


class SmykScraper(_BasePolishRetailerScraper):
    source_name = "smyk.com"
    store_label = "Smyk.com"
    base_url = "https://www.smyk.com"
    free_shipping_threshold = 149.0
    standard_shipping = 12.99

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
        full_scan: bool = False,
    ) -> list[Offer]:
        url = f"https://www.smyk.com/pl/pl/search?q=lego%20{set_number}"
        self._record_url(url)
        status, html, _ = await http_client.get_text(url)
        if status != 200 or not html:
            self._record_failure(f"HTTP {status} on {url}")
            return []
        return self._parse_retail_html(html, set_number, msrp_pln)


class EmpikScraper(_BasePolishRetailerScraper):
    source_name = "empik.com"
    store_label = "Empik.com"
    base_url = "https://www.empik.com"
    free_shipping_threshold = 100.0
    standard_shipping = 11.99

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
        full_scan: bool = False,
    ) -> list[Offer]:
        url = f"https://www.empik.com/szukaj/produkt?q=lego%20{set_number}"
        self._record_url(url)
        status, html, _ = await http_client.get_text(url)
        if status != 200 or not html:
            self._record_failure(f"HTTP {status} on {url}")
            return []
        return self._parse_retail_html(html, set_number, msrp_pln)


class MediaExpertScraper(_BasePolishRetailerScraper):
    source_name = "mediaexpert.pl"
    store_label = "MediaExpert.pl"
    base_url = "https://www.mediaexpert.pl"
    free_shipping_threshold = 99.0
    standard_shipping = 10.99

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
        full_scan: bool = False,
    ) -> list[Offer]:
        url = f"https://www.mediaexpert.pl/search?query[menu_item]=&query[querystring]=lego%20{set_number}"
        self._record_url(url)
        status, html, _ = await http_client.get_text(url)
        if status != 200 or not html:
            self._record_failure(f"HTTP {status} on {url}")
            return []
        return self._parse_retail_html(html, set_number, msrp_pln)
