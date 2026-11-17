from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Optional

from bot.models import Offer, SetMetadata


def parse_price_number(raw: str) -> Optional[float]:
    """Extract a float price from strings like '1 249,99 zł', '399.00 EUR', 'od 450,50'."""
    if not raw:
        return None
    cleaned = raw.replace("\xa0", " ").replace(" ", "")
    match = re.search(r"(\d+(?:[.,]\d{1,2})?)", cleaned)
    if not match:
        return None
    val_str = match.group(1).replace(",", ".")
    try:
        val = float(val_str)
        return val if val > 0 else None
    except ValueError:
        return None


def detect_currency(raw: str, default: str = "PLN") -> str:
    text = (raw or "").upper()
    if "EUR" in text or "€" in text:
        return "EUR"
    if "USD" in text or "$" in text:
        return "USD"
    if "GBP" in text or "£" in text:
        return "GBP"
    if "CZK" in text or "KČ" in text:
        return "CZK"
    if "DKK" in text:
        return "DKK"
    if "PLN" in text or "ZŁ" in text or "ZL" in text:
        return "PLN"
    return default


class BaseScraper(ABC):
    """Base class for all marketplace and aggregator scrapers."""

    source_name: str = "unknown"

    @abstractmethod
    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
    ) -> list[Offer]:
        """Fetch valid new / open-box-sealed-bags original LEGO offers for `set_number`."""
        raise NotImplementedError

    async def fetch_metadata(self, set_number: str) -> Optional[SetMetadata]:
        """Optionally fetch set metadata (name, MSRP on lego.pl, image, retirement date)."""
        return None
