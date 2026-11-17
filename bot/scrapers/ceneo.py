from __future__ import annotations

import logging
import re
from typing import Optional
from bs4 import BeautifulSoup

from bot.models import Offer
from bot.scrapers.base import BaseScraper, parse_price_number
from bot.services.filter import classify_condition, is_authentic_and_complete_set
from bot.services.http_client import http_client

logger = logging.getLogger(__name__)


class CeneoScraper(BaseScraper):
    """
    Parser for ceneo.pl (Poland's largest price comparison engine).
    Searches for 'LEGO {set_number}' and extracts retail offers + delivery prices.
    """

    source_name = "ceneo.pl"

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
    ) -> list[Offer]:
        search_url = f"https://www.ceneo.pl/;szukaj-lego+{set_number}"
        status, html, _ = await http_client.get_text(search_url)
        if status != 200 or not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        offers: list[Offer] = []

        # Parse Ceneo category/search product rows
        cards = soup.select(".cat-prod-row, .product-offer, .js_category-list-item")
        for idx, card in enumerate(cards[:15]):
            title_el = card.select_one(".cat-prod-row__name, .product-offer__title, a.js_seoUrl")
            title = title_el.get_text(" ", strip=True) if title_el else card.get_text(" ", strip=True)[:120]

            if not is_authentic_and_complete_set(
                title=title,
                set_number=set_number,
                msrp_pln=msrp_pln,
            ):
                continue

            cond = classify_condition(title)
            if not cond:
                continue

            price_el = card.select_one(".price, .price-format")
            if not price_el:
                continue
            item_price = parse_price_number(price_el.get_text(" ", strip=True))
            if not item_price:
                continue

            if not is_authentic_and_complete_set(
                title=title,
                set_number=set_number,
                msrp_pln=msrp_pln,
                item_price_pln=item_price,
            ):
                continue

            # Check if Ceneo displays "z dostawą od X zł" or "darmowa wysyłka"
            card_text = card.get_text(" ", strip=True)
            delivery_price: Optional[float] = None
            if re.search(r"darmowa\s+wysyłka|darmowa\s+dostawa", card_text, flags=re.I):
                delivery_price = 0.0
            else:
                with_deliv = re.search(
                    r"z\s+dostawą\s+(?:od\s+)?(\d[\d\s]*[,.]\d{2})\s*z[łl]",
                    card_text,
                    flags=re.I,
                )
                if with_deliv:
                    total_with_deliv = parse_price_number(with_deliv.group(1))
                    if total_with_deliv and total_with_deliv >= item_price:
                        delivery_price = round(total_with_deliv - item_price, 2)

            link_el = card.select_one("a[href]")
            href = str(link_el.get("href", "")) if link_el else ""
            full_url = href if href.startswith("http") else f"https://www.ceneo.pl{href}"
            ext_id = str(card.get("data-pid") or re.sub(r"[^0-9]", "", href)[:12] or f"ceneo-{idx}")

            offers.append(
                Offer(
                    set_number=set_number,
                    title=title,
                    source=self.source_name,
                    store_name="Ceneo.pl Verified Store",
                    url=full_url,
                    external_id=ext_id,
                    item_price_original=item_price,
                    currency_original="PLN",
                    item_price_pln=item_price,
                    delivery_price_pln=delivery_price,
                    delivery_time="1-2 dni robocze",
                    condition=cond,
                )
            )

        return offers
