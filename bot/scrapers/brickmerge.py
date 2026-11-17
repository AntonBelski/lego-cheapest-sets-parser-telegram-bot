from __future__ import annotations

import logging
import re
from typing import Optional
from bs4 import BeautifulSoup

from bot.models import Offer
from bot.scrapers.base import BaseScraper, detect_currency, parse_price_number
from bot.services.currency import currency_converter
from bot.services.filter import is_authentic_and_complete_set
from bot.services.http_client import http_client

logger = logging.getLogger(__name__)


class BrickmergeScraper(BaseScraper):
    """
    Parser for brickmerge.de (European LEGO price aggregator covering Amazon.de,
    Amazon.fr, Amazon.es, Amazon.it, Proshop, Alternate, JB Spielwaren, etc.).
    Converts all EUR/foreign prices to PLN using real-time NBP exchange rates.
    """

    source_name = "brickmerge.de"

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
    ) -> list[Offer]:
        url = f"https://www.brickmerge.de/{set_number}-1"
        status, html, _ = await http_client.get_text(url)
        if status != 200 or not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        offers: list[Offer] = []

        # Brickmerge lists offers in rows/cards with EUR prices
        rows = soup.select(".offer, .price-row, table tr, .bm-offer, div[itemprop='offers']")
        if not rows:
            rows = soup.find_all(["tr", "div"], class_=re.compile(r"offer|shop|price", re.I))

        for idx, row in enumerate(rows[:25]):
            text = row.get_text(" ", strip=True)
            if not any(sym in text for sym in ("€", "EUR", "PLN", "zł")):
                continue

            link_el = row.select_one("a[href]")
            if not link_el:
                continue
            href = str(link_el.get("href", "")).strip()
            if not href:
                continue
            full_url = href if href.startswith("http") else f"https://www.brickmerge.de{href}"

            curr = detect_currency(text, default="EUR")
            price_match = re.search(r"(\d[\d\s]*[,.]\d{2})\s*(?:€|EUR|PLN|zł)", text, flags=re.I)
            if not price_match:
                continue

            orig_price = parse_price_number(price_match.group(1))
            if not orig_price:
                continue

            item_price_pln = await currency_converter.to_pln(orig_price, curr)

            # Estimate EU shipping or parse if explicitly shown
            delivery_pln: Optional[float] = None
            ship_match = re.search(
                r"(?:Versand|Shipping|Dostawa)[:\s+]*(\d+[,.]\d{2})\s*(?:€|EUR)",
                text,
                flags=re.I,
            )
            if ship_match:
                ship_orig = parse_price_number(ship_match.group(1))
                if ship_orig is not None:
                    delivery_pln = await currency_converter.to_pln(ship_orig, curr)
            elif re.search(r"versandkostenfrei|free shipping", text, flags=re.I):
                # Free domestic DE shipping; Amazon.de to PL with Prime/threshold is often 0 PLN or ~39 PLN
                delivery_pln = None

            if not is_authentic_and_complete_set(
                title=f"LEGO {set_number} {text[:120]}",
                set_number=set_number,
                msrp_pln=msrp_pln,
                item_price_pln=item_price_pln,
            ):
                continue

            store_label = "EU Shop (Brickmerge)"
            for known_shop in (
                "Amazon.de",
                "Amazon.fr",
                "Amazon.es",
                "Amazon.it",
                "Alternate",
                "Proshop",
                "JB Spielwaren",
                "Alza",
                "Galaxus",
            ):
                if known_shop.lower() in text.lower():
                    store_label = f"{known_shop} (via Brickmerge)"
                    break

            ext_id = re.sub(r"[^a-zA-Z0-9_-]", "", href[-28:]) or f"bm-{idx}"
            offers.append(
                Offer(
                    set_number=set_number,
                    title=f"LEGO {set_number} ({store_label})",
                    source=self.source_name,
                    store_name=store_label,
                    url=full_url,
                    external_id=ext_id,
                    item_price_original=orig_price,
                    currency_original=curr,
                    item_price_pln=item_price_pln,
                    delivery_price_pln=delivery_pln,
                    delivery_time="EU Shipping to PL (2-5 days)",
                    condition="New (Sealed)",
                )
            )

        return offers
