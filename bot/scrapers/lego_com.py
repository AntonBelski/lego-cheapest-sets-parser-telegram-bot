from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from bot.models import Offer
from bot.scrapers.base import BaseScraper
from bot.services.currency import currency_converter
from bot.services.http_client import http_client

logger = logging.getLogger(__name__)


@dataclass
class LegoComProductStatus:
    """Parsed LEGO.com/pl-pl product availability and pricing snapshot."""

    set_number: str
    title: str
    url: str
    image_url: Optional[str]
    availability_status: str
    availability_text: str
    can_add_to_bag: bool
    price_pln: Optional[float]
    list_price_pln: Optional[float]
    on_sale: bool
    currency: str = "PLN"


def _resolve_ref(apollo: dict[str, Any], obj: Any) -> dict[str, Any]:
    """Resolve an Apollo GraphQL `__ref` pointer when present."""
    if isinstance(obj, dict):
        ref = obj.get("__ref")
        if isinstance(ref, str) and ref in apollo and isinstance(apollo[ref], dict):
            return apollo[ref]
        return obj
    return {}


def _extract_price_value(price_obj: dict[str, Any]) -> Optional[float]:
    if not price_obj:
        return None
    formatted_val = price_obj.get("formattedValue")
    if isinstance(formatted_val, (int, float)) and formatted_val > 0:
        return round(float(formatted_val), 2)
    cent_amount = price_obj.get("centAmount")
    if isinstance(cent_amount, (int, float)) and cent_amount > 0:
        return round(float(cent_amount) / 100.0, 2)
    return None


def _extract_ld_json_product(set_number: str, html_text: str) -> Optional[dict[str, Any]]:
    for match in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text,
        re.DOTALL | re.IGNORECASE,
    ):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type")
            is_product = item_type == "Product" or (
                isinstance(item_type, list) and "Product" in item_type
            )
            if not is_product:
                continue
            prod_id = str(item.get("productId") or item.get("sku") or "").strip()
            item_url = str(item.get("@id") or item.get("url") or "")
            if prod_id == str(set_number).strip() or not prod_id or str(set_number) in item_url:
                return item
    return None


class LegoComScraper(BaseScraper):
    """
    Direct scraper for official LEGO.com Poland (`https://www.lego.com/pl-pl/product/<set>`).
    Detects:
    - Live price and sales/discounts
    - Availability transitions (e.g. R_RETIRED / H_OUT_OF_STOCK -> E_AVAILABLE)
    Always bypasses the short-lived HTTP response cache so every-minute midnight
    checks see real-time stock and price changes.
    """

    source_name = "lego.com"

    def __init__(self) -> None:
        super().__init__()
        self.last_statuses: dict[str, LegoComProductStatus] = {}

    @staticmethod
    def parse_product_html(set_number: str, html_text: str) -> Optional[LegoComProductStatus]:
        """Parse `__NEXT_DATA__` Apollo state (with `application/ld+json` fallback) for `set_number`."""
        if not html_text:
            return None

        set_str = str(set_number).strip()
        ld_product = _extract_ld_json_product(set_str, html_text)
        ld_image: Optional[str] = None
        if ld_product:
            img = ld_product.get("image")
            if isinstance(img, str) and img.startswith("http"):
                ld_image = img
            elif isinstance(img, list) and img and isinstance(img[0], str):
                ld_image = img[0]

        next_match = re.search(
            r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            html_text,
            re.DOTALL | re.IGNORECASE,
        )
        if next_match:
            try:
                next_data = json.loads(next_match.group(1))
                apollo = (
                    next_data.get("props", {})
                    .get("pageProps", {})
                    .get("__APOLLO_STATE__", {})
                )
            except Exception:
                apollo = {}

            if isinstance(apollo, dict):
                prod: Optional[dict[str, Any]] = None
                for val in apollo.values():
                    if isinstance(val, dict) and str(val.get("productCode", "")).strip() == set_str:
                        prod = val
                        break

                if prod is not None:
                    variant = _resolve_ref(apollo, prod.get("variant"))
                    attrs = _resolve_ref(apollo, variant.get("attributes"))
                    price_obj = _resolve_ref(apollo, variant.get("price"))
                    list_price_obj = _resolve_ref(apollo, variant.get("listPrice"))

                    price_val = _extract_price_value(price_obj)
                    list_price_val = _extract_price_value(list_price_obj)
                    currency = str(
                        price_obj.get("currencyCode")
                        or list_price_obj.get("currencyCode")
                        or "PLN"
                    ).upper()

                    can_add = bool(attrs.get("canAddToBag")) or bool(
                        attrs.get("vipCanAddToBag")
                    )
                    avail_status = str(attrs.get("availabilityStatus") or "").strip()
                    avail_text = str(attrs.get("availabilityText") or "").strip()
                    on_sale = bool(attrs.get("onSale"))

                    slug = str(prod.get("slug") or "").strip()
                    canonical_url = (
                        f"https://www.lego.com/pl-pl/product/{slug}"
                        if slug
                        else f"https://www.lego.com/pl-pl/product/{set_str}"
                    )

                    image_url = ld_image
                    if not image_url:
                        for img_key in ("baseImgUrl", "primaryImage"):
                            cand_img = prod.get(img_key)
                            if isinstance(cand_img, str) and cand_img.startswith("http"):
                                image_url = cand_img
                                break

                    raw_name = str(prod.get("name") or f"LEGO {set_str}").strip()
                    title = (
                        raw_name
                        if set_str in raw_name
                        else f"LEGO {set_str} {raw_name}"
                    )

                    return LegoComProductStatus(
                        set_number=set_str,
                        title=title,
                        url=canonical_url,
                        image_url=image_url,
                        availability_status=avail_status or ("E_AVAILABLE" if can_add else "UNKNOWN"),
                        availability_text=avail_text,
                        can_add_to_bag=can_add,
                        price_pln=price_val,
                        list_price_pln=list_price_val,
                        on_sale=on_sale,
                        currency=currency,
                    )

        # Fallback: application/ld+json Product schema
        if ld_product:
            offers_raw = ld_product.get("offers")
            offer_obj: dict[str, Any] = {}
            if isinstance(offers_raw, list) and offers_raw and isinstance(offers_raw[0], dict):
                offer_obj = offers_raw[0]
            elif isinstance(offers_raw, dict):
                offer_obj = offers_raw

            price_val: Optional[float] = None
            raw_price = offer_obj.get("price")
            if raw_price is not None:
                try:
                    price_val = round(float(str(raw_price).replace(",", ".")), 2)
                except ValueError:
                    price_val = None

            currency = str(offer_obj.get("priceCurrency") or "PLN").upper()
            avail_url = str(offer_obj.get("availability") or "")
            avail_lower = avail_url.lower()
            can_add = any(tok in avail_lower for tok in ("instock", "backorder", "preorder"))
            if "discontinued" in avail_lower:
                avail_status = "R_RETIRED"
            elif "outofstock" in avail_lower:
                avail_status = "H_OUT_OF_STOCK"
            elif can_add:
                avail_status = "E_AVAILABLE"
            else:
                avail_status = "UNKNOWN"

            canonical_url = str(
                ld_product.get("@id")
                or ld_product.get("url")
                or f"https://www.lego.com/pl-pl/product/{set_str}"
            ).strip()
            raw_name = str(ld_product.get("name") or f"LEGO {set_str}").strip()
            title = raw_name if set_str in raw_name else f"LEGO {set_str} {raw_name}"

            return LegoComProductStatus(
                set_number=set_str,
                title=title,
                url=canonical_url,
                image_url=ld_image,
                availability_status=avail_status,
                availability_text=avail_url.rsplit("/", 1)[-1] if avail_url else "",
                can_add_to_bag=can_add,
                price_pln=price_val,
                list_price_pln=price_val,
                on_sale=False,
                currency=currency,
            )

        return None

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
        full_scan: bool = False,
    ) -> list[Offer]:
        self.last_statuses.pop(set_number, None)
        url = f"https://www.lego.com/pl-pl/product/{set_number}"
        self._record_url(url)

        # Always bypass response cache so midnight 1-minute checks reflect live stock/prices
        status, html_text, _ = await http_client.get_text(url, use_cache=False)
        if status == 404:
            return []
        if status != 200 or not html_text:
            self._record_failure(f"HTTP {status} fetching {url}")
            return []

        prod = self.parse_product_html(set_number, html_text)
        if prod is None:
            self._record_failure(f"Could not parse product payload for {set_number} on lego.com")
            return []

        if prod.price_pln is not None and prod.currency != "PLN":
            prod.price_pln = await currency_converter.to_pln(prod.price_pln, prod.currency)
            if prod.list_price_pln is not None:
                prod.list_price_pln = await currency_converter.to_pln(
                    prod.list_price_pln, prod.currency
                )

        self.last_statuses[set_number] = prod

        if not prod.can_add_to_bag or prod.price_pln is None or prod.price_pln <= 0:
            return []

        delivery_pln = 0.0 if prod.price_pln >= 200.0 else 24.99
        offer = Offer(
            set_number=set_number,
            title=prod.title,
            source=self.source_name,
            store_name="LEGO.com",
            url=prod.url,
            external_id=f"lego-pl-{set_number}",
            item_price_original=prod.price_pln,
            currency_original=prod.currency,
            item_price_pln=prod.price_pln,
            delivery_price_pln=delivery_pln,
            condition="New (Sealed)",
            image_url=prod.image_url,
        )
        if prod.on_sale and prod.list_price_pln and prod.list_price_pln > prod.price_pln + 1.0:
            list_deliv = 0.0 if prod.list_price_pln >= 200.0 else 24.99
            offer.previous_price_pln = round(prod.list_price_pln + list_deliv, 2)
        return [offer]
