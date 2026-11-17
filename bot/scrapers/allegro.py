from __future__ import annotations

import base64
import logging
import re
import time
from typing import Optional
from bs4 import BeautifulSoup
import httpx

from bot.config import settings
from bot.models import Offer
from bot.scrapers.base import BaseScraper, parse_price_number
from bot.services.currency import currency_converter
from bot.services.filter import classify_condition, is_authentic_and_complete_set
from bot.services.http_client import http_client

logger = logging.getLogger(__name__)


class AllegroLokalnieScraper(BaseScraper):
    """
    Parser for allegrolokalnie.pl (secondary marketplace for private sellers in Poland).
    Searches for new & open-box-sealed-bags LEGO sets and parses delivery cost.
    """

    source_name = "allegrolokalnie.pl"

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
    ) -> list[Offer]:
        url = (
            f"https://allegrolokalnie.pl/oferty/q/lego%20{set_number}"
            f"?stan=nowe&sort=price-asc"
        )
        status, html, _ = await http_client.get_text(url)
        if status != 200 or not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        offers: list[Offer] = []

        cards = soup.select("article, a.mlc-card, .mlc-itembox, [data-testid='offer-card']")
        for idx, card in enumerate(cards[:25]):
            text = card.get_text(" ", strip=True)
            title_el = card.select_one("h2, h3, .mlc-itembox__title")
            title = title_el.get_text(" ", strip=True) if title_el else text[:120]

            price_el = card.select_one(".ml-offer-price__dollars, .mlc-itembox__price, [itemprop='price']")
            if price_el:
                item_price = parse_price_number(price_el.get_text(" ", strip=True))
            else:
                m = re.search(r"(\d[\d\s]*[,.]?\d*)\s*z[łl]", text, flags=re.I)
                item_price = parse_price_number(m.group(1)) if m else None

            if not item_price:
                continue

            if not is_authentic_and_complete_set(
                title=title,
                set_number=set_number,
                msrp_pln=msrp_pln,
                item_price_pln=item_price,
            ):
                continue

            cond = classify_condition(title=title, description=text)
            if not cond:
                continue

            href = card.get("href")
            if not href:
                a_tag = card.select_one("a[href]")
                href = a_tag.get("href") if a_tag else None
            if not href:
                continue
            href_str = str(href).strip()
            full_url = (
                href_str
                if href_str.startswith("http")
                else f"https://allegrolokalnie.pl{href_str}"
            )

            # Parse delivery or Smart! badge
            delivery_price: Optional[float] = None
            if "smart" in text.lower() or "darmowa dostawa" in text.lower():
                delivery_price = 0.0
            else:
                with_deliv = re.search(
                    r"(\d[\d\s]*[,.]\d{2})\s*z[łl]\s*z\s*dostawą",
                    text,
                    flags=re.I,
                )
                if with_deliv:
                    total_d = parse_price_number(with_deliv.group(1))
                    if total_d and total_d >= item_price:
                        delivery_price = round(total_d - item_price, 2)

            ext_id = re.sub(r"[^a-zA-Z0-9_-]", "", href_str[-24:]) or f"alok-{idx}"
            offers.append(
                Offer(
                    set_number=set_number,
                    title=title,
                    source=self.source_name,
                    store_name="Allegro Lokalnie Seller",
                    url=full_url,
                    external_id=ext_id,
                    item_price_original=item_price,
                    currency_original="PLN",
                    item_price_pln=item_price,
                    delivery_price_pln=delivery_price,
                    delivery_time="1-2 dni (Paczkomat / Kurier)",
                    condition=cond,
                )
            )

        return offers


class AllegroScraper(BaseScraper):
    """
    Parser for allegro.pl:
    1) Primary mode: Uses official Allegro REST API (`/offers/listing`) with OAuth2
       Client Credentials when `ALLEGRO_CLIENT_ID` & `ALLEGRO_CLIENT_SECRET` are set.
    2) Fallback mode: Uses browser-impersonated HTML/JSON-LD extraction on allegro.pl.
    """

    source_name = "allegro.pl"

    def __init__(self) -> None:
        self._oauth_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    async def _get_oauth_token(self) -> Optional[str]:
        if not settings.allegro_client_id or not settings.allegro_client_secret:
            return None
        now = time.time()
        if self._oauth_token and now < self._token_expires_at:
            return self._oauth_token

        creds = f"{settings.allegro_client_id}:{settings.allegro_client_secret}"
        b64 = base64.b64encode(creds.encode("utf-8")).decode("ascii")
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.post(
                    "https://allegro.pl/auth/oauth/token?grant_type=client_credentials",
                    headers={"Authorization": f"Basic {b64}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self._oauth_token = data.get("access_token")
                    expires_in = int(data.get("expires_in", 3600))
                    self._token_expires_at = now + expires_in - 60
                    return self._oauth_token
        except Exception as exc:
            logger.warning("Allegro OAuth token request failed: %s", exc)
        return None

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
    ) -> list[Offer]:
        # 1. Try Official Allegro REST API if configured
        token = await self._get_oauth_token()
        if token:
            api_offers = await self._fetch_via_rest_api(set_number, token, msrp_pln)
            if api_offers:
                return api_offers

        # 2. Fallback: Browser-impersonated listing page
        url = f"https://allegro.pl/listing?string=lego%20{set_number}&stan=nowe&order=p"
        status, html, _ = await http_client.get_text(url)
        if status != 200 or not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        offers: list[Offer] = []

        articles = soup.select("article[data-analytics-view-value], article")
        for idx, art in enumerate(articles[:25]):
            text = art.get_text(" ", strip=True)
            a_tag = art.select_one("h2 a[href], a[href*='/oferta/']")
            if not a_tag:
                continue
            title = a_tag.get_text(" ", strip=True) or text[:120]
            href = str(a_tag.get("href", "")).strip()
            if not href:
                continue
            full_url = href if href.startswith("http") else f"https://allegro.pl{href}"

            price_matches = re.findall(r"(\d[\d\s]*[,.]\d{2})\s*z[łl]", text, flags=re.I)
            if not price_matches:
                continue
            item_price = parse_price_number(price_matches[0])
            if not item_price:
                continue

            if not is_authentic_and_complete_set(
                title=title,
                set_number=set_number,
                msrp_pln=msrp_pln,
                item_price_pln=item_price,
            ):
                continue

            cond = classify_condition(title=title, description=text)
            if not cond:
                continue

            delivery_price: Optional[float] = None
            with_deliv = re.search(
                r"(\d[\d\s]*[,.]\d{2})\s*z[łl]\s*z\s*dostawą",
                text,
                flags=re.I,
            )
            if with_deliv:
                total_d = parse_price_number(with_deliv.group(1))
                if total_d and total_d >= item_price:
                    delivery_price = round(total_d - item_price, 2)
            elif "smart" in text.lower() or "darmowa dostawa" in text.lower():
                delivery_price = 0.0

            ext_id = str(art.get("data-analytics-view-value") or re.sub(r"[^0-9]", "", href)[-14:] or f"alg-{idx}")
            offers.append(
                Offer(
                    set_number=set_number,
                    title=title,
                    source=self.source_name,
                    store_name="Allegro.pl Seller",
                    url=full_url,
                    external_id=ext_id,
                    item_price_original=item_price,
                    currency_original="PLN",
                    item_price_pln=item_price,
                    delivery_price_pln=delivery_price,
                    delivery_time="1-2 dni robocze (Allegro Smart! / InPost)",
                    condition=cond,
                )
            )

        return offers

    async def _fetch_via_rest_api(
        self,
        set_number: str,
        token: str,
        msrp_pln: Optional[float],
    ) -> list[Offer]:
        api_url = (
            f"https://api.allegro.pl/offers/listing"
            f"?phrase=LEGO%20{set_number}&sort=+withDeliveryPrice&limit=30"
        )
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    api_url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.allegro.public.v1+json",
                    },
                )
                if resp.status_code != 200:
                    return []
                data = resp.json()
        except Exception as exc:
            logger.debug("Allegro REST API listing failed: %s", exc)
            return []

        items_obj = data.get("items", {})
        all_items = (items_obj.get("promoted", []) or []) + (items_obj.get("regular", []) or [])
        offers: list[Offer] = []

        for item in all_items:
            if not isinstance(item, dict):
                continue
            ext_id = str(item.get("id", ""))
            title = str(item.get("name", "")).strip()
            selling_mode = item.get("sellingMode", {}) or {}
            price_obj = selling_mode.get("price", {}) or {}
            amount = float(price_obj.get("amount", 0) or 0)
            currency = str(price_obj.get("currency", "PLN") or "PLN")
            if amount <= 0:
                continue

            item_price_pln = await currency_converter.to_pln(amount, currency)
            if not is_authentic_and_complete_set(
                title=title,
                set_number=set_number,
                msrp_pln=msrp_pln,
                item_price_pln=item_price_pln,
            ):
                continue

            cond = classify_condition(title=title)
            if not cond:
                continue

            deliv_obj = (item.get("delivery", {}) or {}).get("lowestPrice", {}) or {}
            deliv_amt = deliv_obj.get("amount")
            deliv_curr = str(deliv_obj.get("currency", "PLN") or "PLN")
            delivery_pln: Optional[float] = None
            if deliv_amt is not None:
                delivery_pln = await currency_converter.to_pln(float(deliv_amt), deliv_curr)

            images = item.get("images", []) or []
            img_url = str(images[0].get("url", "")) if images and isinstance(images[0], dict) else None

            offers.append(
                Offer(
                    set_number=set_number,
                    title=title,
                    source=self.source_name,
                    store_name="Allegro.pl",
                    url=f"https://allegro.pl/oferta/{ext_id}",
                    external_id=ext_id,
                    item_price_original=amount,
                    currency_original=currency,
                    item_price_pln=item_price_pln,
                    delivery_price_pln=delivery_pln,
                    delivery_time="1-2 dni robocze",
                    condition=cond,
                    image_url=img_url,
                )
            )
        return offers
