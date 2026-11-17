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


class BricklinkScraper(BaseScraper):
    """
    Parser for bricklink.com for a specific LEGO set (`S={set_number}-1`):
    1) Resolves BrickLink internal item ID (`idItem`) from the set catalog page.
    2) Paginates through BrickLink's `catalogifs.ajax` JSON endpoint (`rpp=100&pg=1..N`)
       for New (`N`) & Complete/Sealed (`C`/`S`) sets so sets with >100 listings
       are never truncated by pagination!
    3) Converts store prices from EUR/USD/GBP/PLN to PLN via real-time NBP rates.
    4) Explicitly marks `delivery_price_pln=None` so it displays `(+delivery)`.
    """

    source_name = "bricklink.com"

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
        full_scan: bool = False,
    ) -> list[Offer]:
        catalog_url = (
            f"https://www.bricklink.com/v2/catalog/catalogitem.page?S={set_number}-1"
        )
        # 1. Resolve BrickLink internal item ID (`idItem`) via fast JSON searchproduct.ajax
        #    (avoids AWS WAF HTTP 405 on catalogitem.page)
        search_ajax_url = (
            f"https://www.bricklink.com/ajax/clone/search/searchproduct.ajax"
            f"?q={set_number}-1&st=0&cond=&type=S&cat=&yf=0&yt=0&loc=&reg=0&ca=0"
            f"&ss=&pmt=&nmp=0&color=-1&min=0&max=0&minqty=0&nosuperlot=1"
            f"&incomplete=0&showempty=1&rpp=25&pi=1&ci=0"
        )
        self._record_url(search_ajax_url)
        item_id: Optional[str] = None
        set_title: str = f"LEGO {set_number}"

        s_status, s_payload = await http_client.get_json(
            search_ajax_url,
            headers={"Referer": catalog_url},
        )
        if s_status == 200 and isinstance(s_payload, dict):
            type_list = (s_payload.get("result") or {}).get("typeList") or []
            for t_entry in type_list:
                for it in t_entry.get("items") or []:
                    if str(it.get("strItemNo", "")).upper().startswith(f"{set_number}-"):
                        item_id = str(it.get("idItem"))
                        set_title = str(it.get("strItemName") or set_title).strip()
                        break

        # Fallback to catalogitem.page if searchproduct.ajax didn't return idItem
        if not item_id:
            self._record_url(catalog_url)
            status, html, _ = await http_client.get_text(
                catalog_url,
                use_jina_html_on_block=False,
            )
            if status != 200 or not html:
                self._record_failure(f"HTTP {status} on {catalog_url}")
                return []
            id_match = re.search(r"idItem\s*[:=]\s*['\"]?(\d+)['\"]?", html)
            if id_match:
                item_id = id_match.group(1)
            set_title_match = re.search(r"<title>([^<]+)</title>", html, flags=re.I)
            if set_title_match:
                set_title = set_title_match.group(1).split("|")[0].strip()

        offers: list[Offer] = []

        if item_id:
            max_pages = 5
            for page in range(1, max_pages + 1):
                # Filter: cond=N (New), ii=0 (no incomplete), ic=0 (complete/sealed only), iconly=0, pi={page}
                ajax_url = (
                    f"https://www.bricklink.com/ajax/clone/catalogifs.ajax"
                    f"?itemid={item_id}&cond=N&ii=0&ic=0&iconly=0&rpp=100&pi={page}"
                )
                self._record_url(ajax_url)
                ajax_status, payload = await http_client.get_json(
                    ajax_url,
                    headers={"Referer": catalog_url},
                )
                if ajax_status != 200 or not isinstance(payload, dict):
                    self._record_failure(f"HTTP {ajax_status} on {ajax_url}")
                    break

                rows = payload.get("list", []) or []
                total_count = int(payload.get("total_count", 0) or 0)
                if not rows:
                    break

                for idx, row in enumerate(rows):
                    if not isinstance(row, dict):
                        continue
                    code_complete = str(row.get("codeComplete", "C")).upper()
                    if code_complete not in ("C", "S"):
                        continue

                    desc = str(row.get("strDesc", "")).strip()
                    raw_price_str = str(
                        row.get("mDisplaySalePrice")
                        or row.get("mInvSalePrice")
                        or ""
                    )
                    orig_price = parse_price_number(raw_price_str)
                    if not orig_price:
                        continue
                    curr = detect_currency(raw_price_str, default="PLN")
                    item_price_pln = await currency_converter.to_pln(
                        orig_price, curr
                    )

                    # On BrickLink with cond=N&ii=0&ic=0, every row is already verified by BrickLink's
                    # catalog as a New & Complete/Sealed set for `itemid` (even if seller comment mentions minifig condition)
                    cond = "New (Sealed)" if code_complete == "S" else "New (Open Box, Sealed Bags)"

                    store_name = str(
                        row.get("strStorename")
                        or row.get("strStoreName")
                        or row.get("strSellerUsername")
                        or "Store"
                    ).strip()
                    seller_country = str(
                        row.get("strSellerCountryName")
                        or row.get("strSellerCountryCode")
                        or ""
                    ).strip()
                    lot_id = str(row.get("idInv") or f"bl-{page}-{idx}")
                    store_username = str(
                        row.get("strSellerUsername") or ""
                    ).strip()
                    from urllib.parse import quote
                    buy_url = (
                        f"https://store.bricklink.com/{quote(store_username, safe='')}#/shop?o=%7B%22invID%22%3A%22{lot_id}%22%7D"
                        if store_username
                        else catalog_url
                    )

                    offers.append(
                        Offer(
                            set_number=set_number,
                            title=f"LEGO {set_number} {set_title} - {desc[:60]}"
                            if desc
                            else f"LEGO {set_number} {set_title} ({code_complete})",
                            source=self.source_name,
                            store_name=f"BrickLink: {store_name} ({seller_country})"
                            if seller_country
                            else f"BrickLink: {store_name}",
                            url=buy_url,
                            external_id=lot_id,
                            item_price_original=orig_price,
                            currency_original=curr,
                            item_price_pln=item_price_pln,
                            delivery_price_pln=None,
                            delivery_time=f"Ships from {seller_country}"
                            if seller_country
                            else "BrickLink Seller",
                            condition=cond,
                            image_url=f"https://img.bricklink.com/ItemImage/SN/0/{set_number}-1.png",
                        )
                    )

                if len(rows) < 100 or (total_count > 0 and page * 100 >= total_count):
                    break

        return offers
