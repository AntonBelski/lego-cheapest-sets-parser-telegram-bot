from __future__ import annotations

import logging
import re
from typing import Optional
from bs4 import BeautifulSoup

from bot.models import Offer
from bot.scrapers.base import BaseScraper, detect_currency, parse_price_number
from bot.services.currency import currency_converter
from bot.services.filter import (
    classify_condition,
    contains_set_number,
    is_authentic_and_complete_set,
)
from bot.services.http_client import http_client

logger = logging.getLogger(__name__)


class EbayScraper(BaseScraper):
    """
    Parser for ebay.pl (covering Polish & European eBay sellers shipping to Poland).
    - On first scan (`full_scan=True`): Paginates `_pgn=1..3` sorted by lowest Price+Shipping (`_sop=15`)
      plus `_pgn=1` sorted by Newly Listed (`_sop=10`).
    - On every 5-minute scan (`full_scan=False`): Checks BOTH `_sop=10` (Newly Listed) AND `_sop=15`
      (Price + Shipping: lowest first) so neither brand-new listings nor price drops by existing
      sellers are ever missed!
    """

    source_name = "ebay.pl"

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
                    f"https://www.ebay.pl/sch/i.html?_nkw=LEGO+{set_number}"
                    f"&LH_ItemCondition=1000%7C1500&LH_BIN=1&_sop=15&_ipg=120&_pgn={page}"
                )
            urls_to_fetch.append(
                f"https://www.ebay.pl/sch/i.html?_nkw=LEGO+{set_number}"
                f"&LH_ItemCondition=1000%7C1500&LH_BIN=1&_sop=10&_ipg=120&_pgn=1"
            )
        else:
            # 5-minute run: check Newly Listed (_sop=10) AND Lowest Price+Shipping (_sop=15)
            urls_to_fetch = [
                f"https://www.ebay.pl/sch/i.html?_nkw=LEGO+{set_number}"
                f"&LH_ItemCondition=1000%7C1500&LH_BIN=1&_sop=10&_ipg=120&_pgn=1",
                f"https://www.ebay.pl/sch/i.html?_nkw=LEGO+{set_number}"
                f"&LH_ItemCondition=1000%7C1500&LH_BIN=1&_sop=15&_ipg=120&_pgn=1",
            ]

        offers: list[Offer] = []
        any_200 = False

        for url in urls_to_fetch:
            self._record_url(url)
            status, html, _ = await http_client.get_text(url)
            if status != 200 or not html:
                self._record_failure(f"HTTP {status} on {url}")
                continue
            any_200 = True

            page_offers, count_raw_items = await self._parse_ebay_html(
                html, set_number, msrp_pln
            )
            offers.extend(page_offers)

            # Stop pagination early if this page had fewer than 20 raw items
            if full_scan and count_raw_items < 20:
                break

        if any_200:
            self.last_error = None
        return offers

    async def _parse_ebay_html(
        self,
        html: str,
        set_number: str,
        msrp_pln: Optional[float],
    ) -> tuple[list[Offer], int]:
        soup = BeautifulSoup(html, "lxml")
        offers: list[Offer] = []

        items = soup.select("li.s-item, div.s-item__wrapper")
        for idx, item in enumerate(items):
            title_el = item.select_one(".s-item__title")
            if not title_el:
                continue
            title = title_el.get_text(" ", strip=True)
            title = re.sub(r"^Nowa oferta[:\s]*", "", title, flags=re.I).strip()
            if not title or "shop on ebay" in title.lower():
                continue

            if not contains_set_number(title, set_number):
                continue

            link_el = item.select_one("a.s-item__link[href]")
            if not link_el:
                continue
            href = str(link_el.get("href", "")).split("?")[0].strip()
            if not href:
                continue

            price_el = item.select_one(".s-item__price")
            if not price_el:
                continue
            price_raw = price_el.get_text(" ", strip=True)
            if " do " in price_raw.lower() or " to " in price_raw.lower():
                continue

            orig_price = parse_price_number(price_raw)
            if not orig_price:
                continue
            curr = detect_currency(price_raw, default="PLN")
            item_price_pln = await currency_converter.to_pln(orig_price, curr)

            subtitle_el = item.select_one(".s-item__subtitle, .SECONDARY_INFO")
            cond_raw = subtitle_el.get_text(" ", strip=True) if subtitle_el else ""

            if not is_authentic_and_complete_set(
                title=title,
                description=cond_raw,
                set_number=set_number,
                msrp_pln=msrp_pln,
                item_price_pln=item_price_pln,
            ):
                continue

            cond = classify_condition(title=title, raw_condition=cond_raw)
            if not cond:
                continue

            ship_el = item.select_one(".s-item__shipping, .s-item__logisticsCost")
            delivery_pln: Optional[float] = None
            if ship_el:
                ship_txt = ship_el.get_text(" ", strip=True)
                if re.search(r"bezpłatna|darmowa|free", ship_txt, flags=re.I):
                    delivery_pln = 0.0
                else:
                    ship_num = parse_price_number(ship_txt)
                    if ship_num is not None:
                        ship_curr = detect_currency(ship_txt, default=curr)
                        delivery_pln = await currency_converter.to_pln(
                            ship_num, ship_curr
                        )

            img_el = item.select_one(".s-item__image-img, img")
            img_url = str(img_el.get("src", "")) if img_el else None

            ext_id_match = re.search(r"/itm/(?:[^/]+/)?(\d+)", href)
            ext_id = ext_id_match.group(1) if ext_id_match else f"ebay-{idx}"

            offers.append(
                Offer(
                    set_number=set_number,
                    title=title,
                    source=self.source_name,
                    store_name="eBay Seller (ships to PL)",
                    url=href,
                    external_id=ext_id,
                    item_price_original=orig_price,
                    currency_original=curr,
                    item_price_pln=item_price_pln,
                    delivery_price_pln=delivery_pln,
                    delivery_time="EU / International (3-7 dni)",
                    condition=cond,
                    image_url=img_url,
                )
            )

        return offers, len(items)
