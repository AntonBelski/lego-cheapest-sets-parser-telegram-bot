from __future__ import annotations

import logging
from typing import Optional

from bot.models import Offer
from bot.scrapers.base import BaseScraper
from bot.services.currency import currency_converter
from bot.services.filter import (
    classify_condition,
    contains_set_number,
    is_authentic_and_complete_set,
)
from bot.services.http_client import http_client

logger = logging.getLogger(__name__)


class OlxScraper(BaseScraper):
    """
    Parser for olx.pl using OLX Poland's official JSON API (`/api/v1/offers/`).
    - On first scan (`full_scan=True`): Paginates `offset=0, 40, 80` (`limit=40`)
      plus newest offers (`sort_by=created_at:desc`).
    - On every 5-minute scan (`full_scan=False`): Queries BOTH `sort_by=created_at:desc`
      (newest listings) AND `sort_by=filter_float_price:asc` (lowest prices, catching
      price reductions by existing sellers).
    """

    source_name = "olx.pl"

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
        full_scan: bool = False,
    ) -> list[Offer]:
        # Always check pages offset=0 and offset=40 sorted by lowest price + offset=0 newest
        # so offer counts remain consistent across every 5-minute scan
        offsets = (0, 40, 80) if full_scan else (0, 40)
        urls_to_fetch: list[str] = [
            f"https://www.olx.pl/api/v1/offers/"
            f"?offset={offset}&limit=40&query=lego%20{set_number}&sort_by=filter_float_price%3Aasc"
            for offset in offsets
        ]
        urls_to_fetch.append(
            f"https://www.olx.pl/api/v1/offers/"
            f"?offset=0&limit=40&query=lego%20{set_number}&sort_by=created_at%3Adesc"
        )

        offers: list[Offer] = []
        any_200 = False

        for api_url in urls_to_fetch:
            self._record_url(api_url)
            status, payload = await http_client.get_json(api_url)
            if status != 200 or not isinstance(payload, dict):
                self._record_failure(f"HTTP {status} on {api_url}")
                continue
            any_200 = True

            items = payload.get("data", [])
            if not isinstance(items, list):
                continue

            for item in items:
                if not isinstance(item, dict):
                    continue

                ext_id = str(item.get("id", ""))
                title = str(item.get("title", "")).strip()
                description = str(item.get("description", "")).strip()
                url = str(item.get("url", "")).strip()
                if not title or not url:
                    continue

                if not contains_set_number(title, set_number):
                    continue

                price_val: Optional[float] = None
                currency = "PLN"
                raw_state = ""

                for param in item.get("params", []):
                    if not isinstance(param, dict):
                        continue
                    key = param.get("key")
                    val = param.get("value", {})
                    if key == "price" and isinstance(val, dict):
                        price_val = float(val.get("value", 0) or 0)
                        currency = str(val.get("currency", "PLN") or "PLN")
                    elif key == "state" and isinstance(val, dict):
                        raw_state = str(val.get("key", "") or val.get("label", ""))

                if not price_val or price_val <= 0:
                    continue

                item_price_pln = await currency_converter.to_pln(price_val, currency)

                if not is_authentic_and_complete_set(
                    title=title,
                    description=description,
                    set_number=set_number,
                    msrp_pln=msrp_pln,
                    item_price_pln=item_price_pln,
                ):
                    continue

                cond = classify_condition(
                    title=title,
                    description=description,
                    raw_condition=raw_state,
                )
                if not cond:
                    continue

                delivery_info = item.get("delivery", {})
                rock = (
                    delivery_info.get("rock", {})
                    if isinstance(delivery_info, dict)
                    else {}
                )
                olx_delivery_active = (
                    bool(rock.get("active")) if isinstance(rock, dict) else False
                )

                delivery_price_pln: Optional[float] = (
                    13.99 if olx_delivery_active else None
                )
                delivery_time = (
                    "Paczkomat InPost 1-2 dni" if olx_delivery_active else None
                )

                photos = item.get("photos", [])
                img_url: Optional[str] = None
                if photos and isinstance(photos[0], dict):
                    link_tmpl = str(photos[0].get("link", ""))
                    if link_tmpl:
                        img_url = link_tmpl.replace("{width}", "800").replace(
                            "{height}", "600"
                        )

                offers.append(
                    Offer(
                        set_number=set_number,
                        title=title,
                        source=self.source_name,
                        store_name="OLX.pl Seller",
                        url=url,
                        external_id=ext_id or url[-20:],
                        item_price_original=price_val,
                        currency_original=currency,
                        item_price_pln=item_price_pln,
                        delivery_price_pln=delivery_price_pln,
                        delivery_time=delivery_time,
                        condition=cond,
                        image_url=img_url,
                    )
                )

            if "filter_float_price" in api_url and len(items) < 40:
                continue

        if any_200:
            self.last_error = None
        return offers
