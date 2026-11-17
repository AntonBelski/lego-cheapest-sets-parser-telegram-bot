from __future__ import annotations

import logging
from typing import Optional

from bot.models import Offer
from bot.scrapers.base import BaseScraper
from bot.services.currency import currency_converter
from bot.services.filter import classify_condition, is_authentic_and_complete_set
from bot.services.http_client import http_client

logger = logging.getLogger(__name__)


class OlxScraper(BaseScraper):
    """
    Parser for olx.pl using OLX Poland's official JSON API (`/api/v1/offers/`).
    Filters for:
    - Original LEGO sets matching `set_number`
    - Condition: New (`state=new`) OR Open Box with Sealed Bags (`woreczki zamknięte`)
    - Delivery: Uses OLX Przesyłka (InPost Paczkomat ~11.99 PLN) when enabled on the listing,
      or sets `delivery_price_pln=None` so it explicitly renders `(+delivery)`.
    """

    source_name = "olx.pl"

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
    ) -> list[Offer]:
        api_url = (
            f"https://www.olx.pl/api/v1/offers/"
            f"?offset=0&limit=30&query=lego%20{set_number}&sort_by=filter_float_price%3Aasc"
        )
        status, payload = await http_client.get_json(api_url)
        if status != 200 or not isinstance(payload, dict):
            return []

        items = payload.get("data", [])
        if not isinstance(items, list):
            return []

        offers: list[Offer] = []
        for item in items:
            if not isinstance(item, dict):
                continue

            ext_id = str(item.get("id", ""))
            title = str(item.get("title", "")).strip()
            description = str(item.get("description", "")).strip()
            url = str(item.get("url", "")).strip()
            if not title or not url:
                continue

            # Extract price & currency from OLX parameters
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

            # Check OLX Delivery (Przesyłka OLX)
            delivery_info = item.get("delivery", {})
            rock = delivery_info.get("rock", {}) if isinstance(delivery_info, dict) else {}
            olx_delivery_active = bool(rock.get("active")) if isinstance(rock, dict) else False

            # When Przesyłka OLX is enabled, standard InPost Paczkomat on OLX is ~11.99 PLN (plus service fee)
            # When not enabled on the listing, delivery_price_pln=None -> displays "(+delivery)"
            delivery_price_pln: Optional[float] = 13.99 if olx_delivery_active else None
            delivery_time = "Paczkomat InPost 1-2 dni" if olx_delivery_active else None

            # Extract first photo if present
            photos = item.get("photos", [])
            img_url: Optional[str] = None
            if photos and isinstance(photos[0], dict):
                link_tmpl = str(photos[0].get("link", ""))
                if link_tmpl:
                    img_url = link_tmpl.replace("{width}", "800").replace("{height}", "600")

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

        return offers
