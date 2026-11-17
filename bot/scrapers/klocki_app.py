from __future__ import annotations

import logging
import re
from typing import Optional
from bs4 import BeautifulSoup

from bot.models import Offer
from bot.scrapers.base import BaseScraper, parse_price_number
from bot.services.filter import is_authentic_and_complete_set
from bot.services.http_client import http_client

logger = logging.getLogger(__name__)


class KlockiAppScraper(BaseScraper):
    """
    Parser for klocki.app (Polish LEGO price comparison aggregator).
    Extracts live offers from Polish stores with shipping cost to Poland.
    """

    source_name = "klocki.app"

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
    ) -> list[Offer]:
        url = f"https://klocki.app/zestaw/lego-{set_number}"
        status, html, _ = await http_client.get_text(url)
        if status != 200 or not html:
            # Try search fallback
            search_url = f"https://klocki.app/szukaj?q={set_number}"
            status, html, _ = await http_client.get_text(search_url)
            if status != 200 or not html:
                return []

        soup = BeautifulSoup(html, "lxml")
        offers: list[Offer] = []

        for idx, card in enumerate(soup.select("a[href], .offer-item, .price-row, tr")):
            text = card.get_text(" ", strip=True)
            if "zł" not in text.lower():
                continue
            price_match = re.search(r"(\d[\d\s]*[,.]\d{2})\s*z[łl]", text, flags=re.I)
            if not price_match:
                continue
            item_price = parse_price_number(price_match.group(1))
            if not item_price:
                continue

            href = card.get("href")
            if not href:
                a_tag = card.select_one("a[href]")
                href = a_tag.get("href") if a_tag else None
            if not href:
                continue
            href_str = str(href).strip()
            full_url = href_str if href_str.startswith("http") else f"https://klocki.app{href_str}"

            delivery_price: Optional[float] = None
            if re.search(r"darmowa|gratis|0,00\s*z[łl]\s*dostawa", text, flags=re.I):
                delivery_price = 0.0
            else:
                deliv_match = re.search(
                    r"(?:dostawa|wysyłka)[:\s+]*(\d+[,.]\d{2})\s*z[łl]",
                    text,
                    flags=re.I,
                )
                if deliv_match:
                    delivery_price = parse_price_number(deliv_match.group(1))

            if not is_authentic_and_complete_set(
                title=f"LEGO {set_number} {text[:100]}",
                set_number=set_number,
                msrp_pln=msrp_pln,
                item_price_pln=item_price,
            ):
                continue

            ext_id = re.sub(r"[^a-zA-Z0-9_-]", "", href_str[-28:]) or f"ka-{idx}"
            offers.append(
                Offer(
                    set_number=set_number,
                    title=f"LEGO {set_number}",
                    source=self.source_name,
                    store_name="Klocki.app Retailer",
                    url=full_url,
                    external_id=ext_id,
                    item_price_original=item_price,
                    currency_original="PLN",
                    item_price_pln=item_price,
                    delivery_price_pln=delivery_price,
                    delivery_time="1-3 dni robocze",
                    condition="New (Sealed)",
                )
            )

        return offers
