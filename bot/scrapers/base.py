from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Optional

from bot.models import Offer, ScraperSiteReport, SetMetadata


def parse_price_number(raw: str) -> Optional[float]:
    """
    Extract a float price from strings like:
    - '1 249,99 zł' -> 1249.99
    - 'PLN 1,448.58' or 'USD 1,000.00' -> 1448.58 / 1000.00
    - '1.448,58 EUR' -> 1448.58
    - '399.00 EUR' -> 399.00
    """
    if not raw:
        return None
    cleaned = raw.replace("\xa0", " ").replace("\u202f", " ")
    # Match numbers with optional thousands separators (space, comma, or dot) and optional 1-2 decimal digits
    match = re.search(
        r"(\d{1,3}(?:[ ,.]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)",
        cleaned,
    )
    if not match:
        return None
    token = match.group(1).replace(" ", "")
    if "," in token and "." in token:
        if token.rfind(".") > token.rfind(","):
            # e.g. '1,448.58' -> comma is thousands, dot is decimal
            token = token.replace(",", "")
        else:
            # e.g. '1.448,58' -> dot is thousands, comma is decimal
            token = token.replace(".", "").replace(",", ".")
    elif "," in token:
        parts = token.split(",")
        if len(parts[-1]) == 3 and len(parts) >= 2:
            token = "".join(parts)
        else:
            token = token.replace(",", ".")
    elif "." in token:
        parts = token.split(".")
        if len(parts[-1]) == 3 and len(parts) >= 2:
            token = "".join(parts)
    try:
        val = float(token)
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


def deduplicate_offers(offers: list[Offer]) -> list[Offer]:
    """Deduplicate offers from the same scraper (e.g. when fetching both newest & lowest sort)."""
    by_key: dict[tuple[str, str], Offer] = {}
    for off in offers:
        k = (off.source, off.external_id)
        if k not in by_key or off.total_price_pln < by_key[k].total_price_pln:
            by_key[k] = off
    return list(by_key.values())


class BaseScraper(ABC):
    """Base class for all marketplace and aggregator scrapers."""

    source_name: str = "unknown"

    def __init__(self) -> None:
        self.last_urls_scraped: list[str] = []
        self.last_error: Optional[str] = None

    def _reset_run_trace(self) -> None:
        self.last_urls_scraped = []
        self.last_error = None

    def _record_url(self, url: str) -> None:
        if url and url not in self.last_urls_scraped:
            self.last_urls_scraped.append(url)

    def _record_failure(self, msg: str) -> None:
        if not self.last_error:
            self.last_error = msg
        else:
            self.last_error = f"{self.last_error}; {msg}"

    @abstractmethod
    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
        full_scan: bool = False,
    ) -> list[Offer]:
        """Fetch valid new / open-box-sealed-bags original LEGO offers for `set_number`."""
        raise NotImplementedError

    async def scrape_with_report(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
        full_scan: bool = False,
    ) -> ScraperSiteReport:
        """Execute `fetch_offers` and return a structured `ScraperSiteReport` with all scraped URLs."""
        self._reset_run_trace()
        try:
            offers = await self.fetch_offers(
                set_number=set_number,
                msrp_pln=msrp_pln,
                full_scan=full_scan,
            )
            deduped = deduplicate_offers(offers)
            success = self.last_error is None
            return ScraperSiteReport(
                set_number=set_number,
                source=self.source_name,
                urls_scraped=list(self.last_urls_scraped),
                success=success,
                offers=deduped,
                error_message=self.last_error,
            )
        except Exception as exc:
            return ScraperSiteReport(
                set_number=set_number,
                source=self.source_name,
                urls_scraped=list(self.last_urls_scraped),
                success=False,
                offers=[],
                error_message=str(exc)[:250],
            )

    async def fetch_metadata(self, set_number: str) -> Optional[SetMetadata]:
        """Optionally fetch set metadata (name, MSRP on lego.pl, image, retirement date)."""
        return None
