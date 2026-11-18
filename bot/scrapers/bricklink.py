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

# Average tracked courier delivery cost to Poland for LEGO sets from personal sellers:
# - ~15 EUR (~65 PLN) for DHL Paket / DPD / GLS across the EU
# - ~18 PLN for InPost / courier within Poland
ESTIMATED_EU_DELIVERY_PLN = 65.0
ESTIMATED_PL_DELIVERY_PLN = 18.0

EU_COUNTRIES: set[str] = {
    "austria", "at",
    "belgium", "be",
    "bulgaria", "bg",
    "croatia", "hr",
    "cyprus", "cy",
    "czech republic", "czechia", "cz",
    "denmark", "dk",
    "estonia", "ee",
    "finland", "fi",
    "france", "fr",
    "germany", "de",
    "greece", "gr",
    "hungary", "hu",
    "ireland", "ie",
    "italy", "it",
    "latvia", "lv",
    "lithuania", "lt",
    "luxembourg", "lu",
    "malta", "mt",
    "netherlands", "nl",
    "poland", "pl",
    "portugal", "pt",
    "romania", "ro",
    "slovakia", "sk",
    "slovenia", "si",
    "spain", "es",
    "sweden", "se",
}


class BricklinkScraper(BaseScraper):
    """
    Parser for bricklink.com for a specific LEGO set (`S={set_number}-1`):
    1) Resolves BrickLink internal item ID (`idItem`) from the set catalog page.
    2) Paginates through BrickLink's `catalogifs.ajax` JSON endpoint (`reg=-1` EU only,
       `cond=N&ii=0&ic=0`, `rpp=100&pi=1..N`).
    3) Converts store prices from EUR/USD/GBP/PLN to PLN via real-time NBP rates.
    4) Adds estimated EU/PL delivery cost (`delivery_is_estimated=True`).
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
                # Filter: cond=N (New), ii=0 (no incomplete), ic=0 (complete/sealed only), reg=-1 (European Union), iconly=0, pi={page}
                ajax_url = (
                    f"https://www.bricklink.com/ajax/clone/catalogifs.ajax"
                    f"?itemid={item_id}&cond=N&ii=0&ic=0&reg=-1&iconly=0&rpp=100&pi={page}"
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

                    seller_country = str(
                        row.get("strSellerCountryName")
                        or row.get("strSellerCountryCode")
                        or ""
                    ).strip()
                    seller_country_code = str(row.get("strSellerCountryCode") or "").strip().lower()
                    if (
                        seller_country.lower() not in EU_COUNTRIES
                        and seller_country_code not in EU_COUNTRIES
                    ):
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

                    est_deliv = (
                        ESTIMATED_PL_DELIVERY_PLN
                        if seller_country.lower() in ("poland", "pl") or seller_country_code == "pl"
                        else ESTIMATED_EU_DELIVERY_PLN
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
                            delivery_price_pln=est_deliv,
                            delivery_is_estimated=True,
                            delivery_time=f"Ships from {seller_country}"
                            if seller_country
                            else "BrickLink Seller (EU)",
                            condition=cond,
                            image_url=f"https://img.bricklink.com/ItemImage/SN/0/{set_number}-1.png",
                        )
                    )

                if len(rows) < 100 or (total_count > 0 and page * 100 >= total_count):
                    break

        return offers
