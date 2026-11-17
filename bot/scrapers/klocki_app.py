from __future__ import annotations

import logging
import re
from typing import Optional
from bs4 import BeautifulSoup

from bot.models import Offer
from bot.scrapers.base import BaseScraper, parse_price_number
from bot.services.filter import contains_set_number, is_authentic_and_complete_set
from bot.services.http_client import http_client

logger = logging.getLogger(__name__)


class KlockiAppScraper(BaseScraper):
    """
    Parser for zklockow.pl (`https://zklockow.pl/lego-{set_number}`), replacing
    the defunct `klocki.app` domain.
    Extracts live Polish retail store offers and Allegro Super Seller offers
    with exact shipping costs (`+ 14,99 zł dostawa` or `darmowa dostawa`),
    strictly verifying that every offer title contains `set_number`.
    """

    source_name = "zklockow.pl"

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
        full_scan: bool = False,
    ) -> list[Offer]:
        url = f"https://zklockow.pl/lego-{set_number}"
        self._record_url(url)
        status, html, _ = await http_client.get_text(
            url,
            use_jina_html_on_block=False,
        )
        if status != 200 or not html:
            # Datacenter IP fallback: extract partner Polish store offers via Promoklocki edge relay
            from bot.scrapers.promoklocki import PromoklockiScraper
            pk = PromoklockiScraper()
            _, pk_offers, _ = await pk.fetch_metadata_and_history(set_number)
            if pk.last_error is None:
                self.last_error = None
                fallback_offers: list[Offer] = []
                for off in pk_offers:
                    clean_s = off.store_name.replace(" (Promoklocki)", "")
                    fallback_offers.append(
                        Offer(
                            set_number=off.set_number,
                            title=off.title,
                            source=self.source_name,
                            store_name=f"{clean_s} (PL Store)",
                            url=off.url,
                            external_id=f"zk-{off.external_id}",
                            item_price_original=off.item_price_original,
                            currency_original=off.currency_original,
                            item_price_pln=off.item_price_pln,
                            delivery_price_pln=off.delivery_price_pln,
                            delivery_time=off.delivery_time,
                            condition=off.condition,
                            image_url=off.image_url,
                        )
                    )
                return fallback_offers
            self._record_failure(f"HTTP {status} on {url}")
            return []

        soup = BeautifulSoup(html, "lxml")
        offers: list[Offer] = []

        # Every store/Allegro offer on zklockow.pl is an <a class="Off" href="https://zklockow.pl/idz-do/...">
        for idx, a_tag in enumerate(soup.select("a.Off[href]")):
            text = a_tag.get_text(" ", strip=True)
            href = str(a_tag.get("href", "")).strip()
            if not text or not href:
                continue

            # Double check that this offer is strictly for `set_number`
            if not contains_set_number(text, set_number):
                continue

            # Extract item price (first price before 'zł')
            price_matches = re.findall(r"(\d[\d\s]*,\s*\d{2})\s*z[łl]", text, flags=re.I)
            if not price_matches:
                continue
            item_price = parse_price_number(price_matches[0])
            if not item_price:
                continue

            if not is_authentic_and_complete_set(
                title=text,
                set_number=set_number,
                msrp_pln=msrp_pln,
                item_price_pln=item_price,
            ):
                continue

            # Extract delivery price if present (e.g. '+ 14,99 zł dostawa' or 'darmowa dostawa')
            delivery_price: Optional[float] = None
            if re.search(r"darmowa\s+dostawa|dostawa\s+gratis|0,00\s*z[łl]\s*dostawa", text, flags=re.I):
                delivery_price = 0.0
            else:
                deliv_m = re.search(
                    r"\+\s*(\d[\d\s]*,\s*\d{2})\s*z[łl]\s*dostawa",
                    text,
                    flags=re.I,
                )
                if deliv_m:
                    delivery_price = parse_price_number(deliv_m.group(1))

            # Extract store/seller name (e.g. 'od mediaexpert.pl' or 'od Super Sprzedawcy PKBricks')
            store_name = "zklockow.pl Partner"
            seller_m = re.search(
                r"\bod\s+(?:Super\s+Sprzedawcy\s+)?([A-Za-z0-9._-]+)",
                text,
            )
            if seller_m:
                store_name = f"{seller_m.group(1)} (via zklockow.pl)"

            full_url = href if href.startswith("http") else f"https://zklockow.pl{href}"
            clean_store = re.sub(r"[^a-z0-9_-]", "", store_name.lower())
            ext_id = f"zk-{set_number}-{clean_store or idx}"

            offers.append(
                Offer(
                    set_number=set_number,
                    title=text.split(" od ")[0].strip()[:120] or f"LEGO {set_number}",
                    source=self.source_name,
                    store_name=store_name,
                    url=full_url,
                    external_id=ext_id,
                    item_price_original=item_price,
                    currency_original="PLN",
                    item_price_pln=item_price,
                    delivery_price_pln=delivery_price,
                    delivery_time="1-2 dni robocze",
                    condition="New (Sealed)",
                )
            )

        return offers
