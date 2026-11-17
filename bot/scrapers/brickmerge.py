from __future__ import annotations

import logging
import re
from typing import Optional
from bs4 import BeautifulSoup

from bot.models import Offer
from bot.scrapers.base import BaseScraper, detect_currency, parse_price_number
from bot.services.currency import currency_converter
from bot.services.filter import contains_set_number, is_authentic_and_complete_set
from bot.services.http_client import http_client

logger = logging.getLogger(__name__)

# Complete classification of Brickmerge.de monitored stores based on direct shipping to Poland:
# 1. Stores that DO ship directly to Poland (with typical shipping cost in EUR if not free)
BRICKMERGE_SHOPS_SHIPPING_TO_POLAND: dict[str, Optional[float]] = {
    "amazon": 0.0,                 # Amazon.de (free shipping to PL on orders >=39 EUR / Prime)
    "amazon.de": 0.0,
    "amazon (fr)": 7.99,           # Amazon.fr ships to Poland (~7.99 EUR)
    "amazon (es)": 7.99,           # Amazon.es ships to Poland (~7.99 EUR)
    "amazon (it)": 7.99,           # Amazon.it ships to Poland (~7.99 EUR)
    "proshop": 4.65,               # Proshop ships directly to Poland (~20 PLN / ~4.65 EUR)
    "alternate": 9.99,             # Alternate ships to Poland
    "jb spielwaren": 12.99,        # JB Spielwaren ships to Poland via DHL (12.99 EUR)
    "brickmo.com": 13.95,          # BRICKMO.com ships to Poland via DHL/GLS (13.95 EUR)
    "steinehelden": 13.99,         # Steinehelden ships to EU/Poland via DHL (13.99 EUR)
    "lucky bricks": 14.95,         # Lucky Bricks ships to EU/Poland via DHL (14.95 EUR)
    "brickcoast": 13.99,           # BrickCoast ships to EU/Poland via DHL (13.99 EUR)
    "ltoys": 15.50,                # LToys ships to EU/Poland
    "collector expert": 14.90,     # Collector Expert ships EU-wide including Poland
    "baustein store": 14.99,       # Baustein Store ships EU-wide including Poland
    "minifiguremaddness.com": 14.99,  # Ships from Ireland to Poland via DHL/UPS
    "disneystore": 9.90,           # DisneyStore.eu ships to Poland
    "conrad": 7.99,                # Conrad operates in EU & Poland
    "toymi.eu": 11.99,             # toymi.eu ships across EU including Poland
    "jacob": 12.99,                # JACOB.de ships within EU
    "ebay.de": None,               # eBay.de (varies by seller -> marked (+delivery))
}

# 2. Stores monitored by Brickmerge that DO NOT ship directly to Polish addresses
#    (Germany / Austria / DACH domestic shipping only — strictly filtered out!)
BRICKMERGE_SHOPS_NO_POLAND_DELIVERY: tuple[str, ...] = (
    "alza",
    "office-partner",
    "kiseba",
    "computersalg",
    "stein-kontor",
    "cw-mobile.de",
    "kaufland.de",
    "otto",
    "mediamarkt",
    "saturn",
    "müller",
    "mueller",
    "lidl.de",
    "netto",
    "rewe",
    "mytime.de",
    "smyths toys",
    "galaxus.de",
    "coolblue.de",
    "thalia",
    "thalia.at",
    "hugendubel.de",
    "bücher.de",
    "buecher.de",
    "baby-markt",
    "baby-walz",
    "baur",
    "barando",
    "duo-shop",
    "idee+spiel",
    "elbbricks",
    "holy brick",
    "itilis",
    "joybuy",
    "playzeug",
    "spielzeug-wiesen",
    "toys for fun",
    "drehscheibe24.at",
    "coolshop",
)


def check_brickmerge_shop_ships_to_poland(shop_name: str) -> tuple[bool, Optional[float]]:
    """
    Check if a Brickmerge merchant ships to Poland.
    Returns (ships_to_poland, estimated_shipping_eur).
    """
    norm = (shop_name or "").strip().lower()
    for blocked in BRICKMERGE_SHOPS_NO_POLAND_DELIVERY:
        if blocked == norm or blocked in norm:
            return False, None
    for allowed, ship_eur in BRICKMERGE_SHOPS_SHIPPING_TO_POLAND.items():
        if allowed == norm or allowed in norm:
            return True, ship_eur
    return False, None


class BrickmergeScraper(BaseScraper):
    """
    Parser for brickmerge.de/{set_number}-1:
    - Extracts all merchant offer rows (`Hier zu <Merchant>!`)
    - Strictly filters OUT stores that do NOT deliver to Poland
    - Converts EUR item & shipping prices to PLN using real-time NBP rates.
    """

    source_name = "brickmerge.de"

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
        full_scan: bool = False,
    ) -> list[Offer]:
        url = f"https://www.brickmerge.de/{set_number}-1"
        self._record_url(url)
        status, html, _ = await http_client.get_text(url)
        if status != 200 or not html:
            self._record_failure(f"HTTP {status} on {url}")
            return []

        soup = BeautifulSoup(html, "lxml")
        page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if not contains_set_number(page_title, set_number):
            return []

        offers: list[Offer] = []

        for idx, row in enumerate(soup.select("div.row")):
            text = row.get_text(" ", strip=True)
            if "Hier zu " not in text or "€" not in text:
                continue

            # Extract merchant name from "Hier zu <Merchant>!"
            shop_m = re.search(r"Hier\s+zu\s+([^!>]+)!", text)
            if not shop_m:
                continue
            shop_name = shop_m.group(1).strip()

            # Check if this Brickmerge store ships to Poland
            ships_to_pl, est_ship_eur = check_brickmerge_shop_ships_to_poland(shop_name)
            if not ships_to_pl:
                continue

            price_m = re.search(r"(\d[\d\s]*[,.]\d{2})\s*€", text)
            if not price_m:
                continue
            orig_price = parse_price_number(price_m.group(1))
            if not orig_price:
                continue

            curr = detect_currency(text, default="EUR")
            item_price_pln = await currency_converter.to_pln(orig_price, curr)

            delivery_pln: Optional[float] = None
            if est_ship_eur is not None:
                delivery_pln = await currency_converter.to_pln(est_ship_eur, "EUR")

            if not is_authentic_and_complete_set(
                title=f"LEGO {set_number} {page_title}",
                set_number=set_number,
                msrp_pln=msrp_pln,
                item_price_pln=item_price_pln,
            ):
                continue

            go_link = row.select_one("a[href*='/go2/'], a[href*='go2i=']")
            href = str(go_link.get("href", "")).strip() if go_link else ""
            full_url = (
                href
                if href.startswith("http")
                else (f"https://www.brickmerge.de{href}" if href else url)
            )
            ext_id = f"bm-{re.sub(r'[^a-z0-9]', '', shop_name.lower())}-{set_number}"

            offers.append(
                Offer(
                    set_number=set_number,
                    title=f"LEGO {set_number} ({shop_name})",
                    source=self.source_name,
                    store_name=f"{shop_name} (via Brickmerge)",
                    url=full_url,
                    external_id=ext_id,
                    item_price_original=orig_price,
                    currency_original=curr,
                    item_price_pln=item_price_pln,
                    delivery_price_pln=delivery_pln,
                    delivery_time="EU Shipping to Poland (3-5 days)",
                    condition="New (Sealed)",
                )
            )

        return offers
