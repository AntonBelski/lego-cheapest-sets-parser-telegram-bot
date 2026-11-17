from __future__ import annotations

import logging
import time
import httpx

logger = logging.getLogger(__name__)

# Reliable fallback rates in case both NBP and ER-API are temporarily unreachable
FALLBACK_RATES_TO_PLN: dict[str, float] = {
    "PLN": 1.0,
    "ZŁ": 1.0,
    "ZL": 1.0,
    "EUR": 4.32,
    "€": 4.32,
    "USD": 4.02,
    "$": 4.02,
    "GBP": 5.15,
    "£": 5.15,
    "CZK": 0.171,
    "KČ": 0.171,
    "DKK": 0.579,
    "SEK": 0.376,
    "NOK": 0.368,
    "CHF": 4.58,
    "HUF": 0.0108,
    "RON": 0.868,
}


class CurrencyConverter:
    """
    Converts foreign currencies (EUR, USD, GBP, CZK, DKK, etc.) to Polish Złoty (PLN)
    using the official Narodowy Bank Polski (NBP) Web API (`api.nbp.pl`), with fallback
    to `open.er-api.com`. Caches rates in memory for 6 hours to minimize API calls.
    """

    def __init__(self, ttl_seconds: int = 21600):
        self.ttl_seconds = ttl_seconds
        self._rates_to_pln: dict[str, float] = dict(FALLBACK_RATES_TO_PLN)
        self._last_updated: float = 0.0

    async def refresh_if_needed(self) -> None:
        now = time.time()
        if (now - self._last_updated) < self.ttl_seconds and self._last_updated > 0:
            return

        # 1. Primary source: Official Narodowy Bank Polski (NBP) Table A
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://api.nbp.pl/api/exchangerates/tables/A/?format=json",
                    headers={"Accept": "application/json"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data and isinstance(data, list) and "rates" in data[0]:
                        for item in data[0]["rates"]:
                            code = str(item.get("code", "")).upper()
                            mid = float(item.get("mid", 0.0))
                            if code and mid > 0:
                                self._rates_to_pln[code] = mid
                        self._rates_to_pln["PLN"] = 1.0
                        self._rates_to_pln["EUR"] = self._rates_to_pln.get("EUR", 4.32)
                        self._rates_to_pln["€"] = self._rates_to_pln["EUR"]
                        self._rates_to_pln["$"] = self._rates_to_pln.get("USD", 4.02)
                        self._rates_to_pln["£"] = self._rates_to_pln.get("GBP", 5.15)
                        self._last_updated = now
                        logger.info(
                            "Updated FX rates from NBP (1 EUR = %.4f PLN, 1 USD = %.4f PLN)",
                            self._rates_to_pln.get("EUR", 0),
                            self._rates_to_pln.get("USD", 0),
                        )
                        return
        except Exception as exc:
            logger.warning("NBP FX rate fetch failed, trying fallback: %s", exc)

        # 2. Secondary source: open.er-api.com
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get("https://open.er-api.com/v6/latest/PLN")
                if resp.status_code == 200:
                    data = resp.json()
                    rates = data.get("rates", {})
                    for code, pln_to_curr in rates.items():
                        if pln_to_curr and float(pln_to_curr) > 0:
                            self._rates_to_pln[code.upper()] = round(1.0 / float(pln_to_curr), 6)
                    self._rates_to_pln["PLN"] = 1.0
                    self._last_updated = now
                    logger.info("Updated FX rates from open.er-api.com")
        except Exception as exc:
            logger.warning("Fallback FX rate fetch failed, using cached/fallback rates: %s", exc)

    async def to_pln(self, amount: float, currency: str) -> float:
        """Convert an amount in `currency` to PLN."""
        if amount <= 0:
            return 0.0
        norm = (currency or "PLN").strip().upper()
        if norm in ("PLN", "ZŁ", "ZL"):
            return round(float(amount), 2)

        await self.refresh_if_needed()
        rate = self._rates_to_pln.get(norm, FALLBACK_RATES_TO_PLN.get(norm, 1.0))
        return round(float(amount) * rate, 2)

    def to_pln_sync(self, amount: float, currency: str) -> float:
        """Synchronous conversion using currently cached rates."""
        if amount <= 0:
            return 0.0
        norm = (currency or "PLN").strip().upper()
        if norm in ("PLN", "ZŁ", "ZL"):
            return round(float(amount), 2)
        rate = self._rates_to_pln.get(norm, FALLBACK_RATES_TO_PLN.get(norm, 1.0))
        return round(float(amount) * rate, 2)

    def get_rate(self, currency: str) -> float:
        norm = (currency or "PLN").strip().upper()
        return self._rates_to_pln.get(norm, FALLBACK_RATES_TO_PLN.get(norm, 1.0))


currency_converter = CurrencyConverter()
