from __future__ import annotations

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


class CeneoScraper(BaseScraper):
    """
    Two-step parser for ceneo.pl:
    1) Searches `https://www.ceneo.pl/;szukaj-lego+{set_number}` and finds the product page
       whose title strictly contains `set_number` (e.g. `/165836290`).
    2) Opens the product page sorted by lowest price (`https://www.ceneo.pl/{pid};0280-0.htm`)
       to scrape ALL available store offers and their exact shipping prices.
    """

    source_name = "ceneo.pl"

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
        full_scan: bool = False,
    ) -> list[Offer]:
        search_url = f"https://www.ceneo.pl/;szukaj-lego+{set_number}"
        self._record_url(search_url)
        status, html, _ = await http_client.get_text(search_url)
        if status != 200 or not html:
            self._record_failure(f"HTTP {status} on {search_url}")
            return []

        soup = BeautifulSoup(html, "lxml")

        # Step 1: Find matching product page ID(s) on Ceneo search results
        product_ids: list[str] = []
        for a_tag in soup.select("a.js_seoUrl, .cat-prod-row__name a, a[href]"):
            txt = a_tag.get_text(" ", strip=True)
            href = str(a_tag.get("href", "")).strip()
            if not contains_set_number(txt, set_number):
                continue
            if not is_authentic_and_complete_set(title=txt, set_number=set_number):
                continue
            m = re.match(r"^/(?:klocki-)?(\d{6,12})(?:[;?#/]|$)", href)
            if m:
                pid = m.group(1)
                if pid not in product_ids:
                    product_ids.append(pid)

        offers: list[Offer] = []

        # Step 2: Open the Ceneo product page sorted by price ascending (;0280-0.htm)
        for pid in product_ids[:2]:
            prod_url = f"https://www.ceneo.pl/{pid};0280-0.htm"
            self._record_url(prod_url)
            p_status, p_html, _ = await http_client.get_text(prod_url)
            if p_status != 200 or not p_html:
                self._record_failure(f"HTTP {p_status} on {prod_url}")
                continue

            psoup = BeautifulSoup(p_html, "lxml")
            offer_nodes = psoup.select(
                ".product-offer__container, li.product-offers__list__item"
            )
            for idx, node in enumerate(offer_nodes):
                node_text = node.get_text(" ", strip=True)
                title_el = node.select_one(
                    ".product-offer__product__offer-details__name, .short-name__txt, a.js_product-offer-link"
                )
                title = (
                    title_el.get_text(" ", strip=True)
                    if title_el
                    else f"LEGO {set_number}"
                )

                # Must strictly contain the exact set number
                if not contains_set_number(f"{title} {node_text}", set_number):
                    continue

                raw_price_attr = node.get("data-price")
                if raw_price_attr:
                    item_price = parse_price_number(str(raw_price_attr))
                else:
                    price_el = node.select_one(".price, .price-format")
                    item_price = (
                        parse_price_number(price_el.get_text(" ", strip=True))
                        if price_el
                        else None
                    )
                if not item_price:
                    continue

                if not is_authentic_and_complete_set(
                    title=title,
                    description=node_text,
                    set_number=set_number,
                    msrp_pln=msrp_pln,
                    item_price_pln=item_price,
                ):
                    continue

                cond = classify_condition(title)
                if not cond:
                    continue

                # Extract delivery cost from Ceneo offer row
                delivery_price: Optional[float] = None
                if re.search(
                    r"darmowa\s+wysyłka|darmowa\s+dostawa|wysyłka\s+0\s*z[łl]",
                    node_text,
                    flags=re.I,
                ):
                    delivery_price = 0.0
                else:
                    ship_m = re.search(
                        r"(?:wysyłka|dostawa)\s+od\s+(\d[\d\s]*[,.]\d{2})\s*z[łl]",
                        node_text,
                        flags=re.I,
                    )
                    if ship_m:
                        delivery_price = parse_price_number(ship_m.group(1))
                    else:
                        with_deliv = re.search(
                            r"z\s+dostawą\s+(?:od\s+)?(\d[\d\s]*[,.]\d{2})\s*z[łl]",
                            node_text,
                            flags=re.I,
                        )
                        if with_deliv:
                            total_d = parse_price_number(with_deliv.group(1))
                            if total_d and total_d >= item_price:
                                delivery_price = round(total_d - item_price, 2)

                shop_domain = str(node.get("data-shopurl") or "").strip()
                if not shop_domain:
                    logo_img = node.select_one("img[alt]")
                    if logo_img and logo_img.get("alt"):
                        shop_domain = str(logo_img.get("alt")).strip()
                store_name = (
                    f"{shop_domain} (via Ceneo)" if shop_domain else "Ceneo.pl Store"
                )

                go_link = node.select_one("a[href*='/Click/'], a.button[href], a[href]")
                href = str(go_link.get("href", "")) if go_link else ""
                full_url = (
                    href
                    if href.startswith("http")
                    else (f"https://www.ceneo.pl{href}" if href else prod_url)
                )
                clean_shop = re.sub(r"[^a-z0-9_-]", "", (shop_domain or f"shop{idx}").lower())
                offer_id = str(
                    node.get("data-offer-id")
                    or f"ceneo-{pid}-{clean_shop}"
                )

                offers.append(
                    Offer(
                        set_number=set_number,
                        title=title,
                        source=self.source_name,
                        store_name=store_name,
                        url=full_url,
                        external_id=offer_id,
                        item_price_original=item_price,
                        currency_original="PLN",
                        item_price_pln=item_price,
                        delivery_price_pln=delivery_price,
                        delivery_time="1-2 dni robocze",
                        condition=cond,
                    )
                )

        return offers
