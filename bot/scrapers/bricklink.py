from __future__ import annotations

import logging
import re
from typing import Optional
from bs4 import BeautifulSoup

from bot.models import Offer
from bot.scrapers.base import BaseScraper, detect_currency, parse_price_number
from bot.services.currency import currency_converter
from bot.services.filter import classify_condition, is_authentic_and_complete_set
from bot.services.http_client import http_client

logger = logging.getLogger(__name__)


class BricklinkScraper(BaseScraper):
    """
    Parser for bricklink.com for a specific LEGO set (`S={set_number}-1`):
    1) Resolves BrickLink internal item ID (`idItem`) from the set catalog page
       or queries BrickLink's `catalogifs.ajax` JSON endpoint for New (`N`) &
       Complete/Sealed (`C`/`S`) sets available for purchase.
    2) Converts store prices from EUR/USD/GBP/PLN to PLN via real-time NBP rates.
    3) Explicitly marks `delivery_price_pln=None` so it displays `(+delivery)` since
       BrickLink sellers calculate shipping by weight/invoice.
    """

    source_name = "bricklink.com"

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
    ) -> list[Offer]:
        catalog_url = f"https://www.bricklink.com/v2/catalog/catalogitem.page?S={set_number}-1"
        status, html, _ = await http_client.get_text(catalog_url)
        if status != 200 or not html:
            return []

        # Extract BrickLink internal idItem for the set
        id_match = re.search(r"idItem\s*[:=]\s*['\"]?(\d+)['\"]?", html)
        set_title_match = re.search(r"<title>([^<]+)</title>", html, flags=re.I)
        set_title = (
            set_title_match.group(1).split("|")[0].strip()
            if set_title_match
            else f"LEGO {set_number}"
        )

        offers: list[Offer] = []

        if id_match:
            item_id = id_match.group(1)
            # Query BrickLink's internal AJAX inventory listing for New (N) Complete/Sealed sets
            ajax_url = (
                f"https://www.bricklink.com/ajax/clone/catalogifs.ajax"
                f"?itemid={item_id}&cond=N&comp=1&rpp=25&iconly=0"
            )
            ajax_status, payload = await http_client.get_json(
                ajax_url,
                headers={"Referer": catalog_url},
            )
            if ajax_status == 200 and isinstance(payload, dict):
                rows = payload.get("list", []) or []
                for idx, row in enumerate(rows[:20]):
                    if not isinstance(row, dict):
                        continue
                    # Reject incomplete sets (codeComplete: 'C' = Complete, 'S' = Sealed, 'B' = Incomplete)
                    code_complete = str(row.get("codeComplete", "C")).upper()
                    if code_complete not in ("C", "S"):
                        continue

                    desc = str(row.get("strDesc", "")).strip()
                    raw_price_str = str(row.get("mDisplaySalePrice") or row.get("mInvSalePrice") or "")
                    orig_price = parse_price_number(raw_price_str)
                    if not orig_price:
                        continue
                    curr = detect_currency(raw_price_str, default="PLN")
                    item_price_pln = await currency_converter.to_pln(orig_price, curr)

                    if not is_authentic_and_complete_set(
                        title=f"LEGO {set_number} {set_title}",
                        description=desc,
                        set_number=set_number,
                        msrp_pln=msrp_pln,
                        item_price_pln=item_price_pln,
                    ):
                        continue

                    cond = "New (Sealed)" if code_complete == "S" else classify_condition(
                        title=set_title,
                        description=desc,
                    )
                    if not cond:
                        continue

                    store_name = str(row.get("strStoreName") or "BrickLink Store").strip()
                    seller_country = str(row.get("strSellerCountryName") or row.get("strSellerCountryCode") or "").strip()
                    lot_id = str(row.get("idInv") or f"bl-{idx}")
                    store_username = str(row.get("strSellerUsername") or "").strip()
                    buy_url = (
                        f"https://store.bricklink.com/{store_username}#/shop?o={{\"invID\":\"{lot_id}\"}}"
                        if store_username
                        else catalog_url
                    )

                    offers.append(
                        Offer(
                            set_number=set_number,
                            title=f"LEGO {set_number} - {desc[:70]}" if desc else f"LEGO {set_number} ({code_complete})",
                            source=self.source_name,
                            store_name=f"{store_name} ({seller_country})" if seller_country else store_name,
                            url=buy_url,
                            external_id=lot_id,
                            item_price_original=orig_price,
                            currency_original=curr,
                            item_price_pln=item_price_pln,
                            delivery_price_pln=None,  # BrickLink shipping is calculated per seller -> "(+delivery)"
                            delivery_time=f"Ships from {seller_country}" if seller_country else "BrickLink Seller",
                            condition=cond,
                            image_url=f"https://img.bricklink.com/ItemImage/SN/0/{set_number}-1.png",
                        )
                    )

        # Fallback: parse price summary from the catalog HTML if AJAX returned no rows
        if not offers:
            soup = BeautifulSoup(html, "lxml")
            text = soup.get_text(" ", strip=True)
            m = re.search(r"New\s*:\s*(?:PLN|EUR|USD|\$|€)\s*(\d[\d.,]*)", text, flags=re.I)
            if m:
                curr = detect_currency(m.group(0), default="PLN")
                p_orig = parse_price_number(m.group(1))
                if p_orig:
                    p_pln = await currency_converter.to_pln(p_orig, curr)
                    if is_authentic_and_complete_set(
                        title=f"LEGO {set_number} {set_title}",
                        set_number=set_number,
                        msrp_pln=msrp_pln,
                        item_price_pln=p_pln,
                    ):
                        offers.append(
                            Offer(
                                set_number=set_number,
                                title=f"LEGO {set_number} {set_title}",
                                source=self.source_name,
                                store_name="BrickLink Marketplace",
                                url=catalog_url,
                                external_id=f"bl-cat-{set_number}",
                                item_price_original=p_orig,
                                currency_original=curr,
                                item_price_pln=p_pln,
                                delivery_price_pln=None,
                                delivery_time="BrickLink Sellers",
                                condition="New (Sealed)",
                                image_url=f"https://img.bricklink.com/ItemImage/SN/0/{set_number}-1.png",
                            )
                        )

        return offers
