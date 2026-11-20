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
from bot.services.filter import (
    classify_condition,
    contains_set_number,
    is_authentic_and_complete_set,
)
from bot.services.http_client import http_client

logger = logging.getLogger(__name__)


class AllegroLokalnieScraper(BaseScraper):
    """
    Parser for allegrolokalnie.pl (secondary marketplace for private sellers in Poland).
    - On first scan (`full_scan=True`): Paginates `page=1..3` sorted by price ascending
      plus `page=1` sorted by newest (`sort=startingTime-desc`).
    - On 5-minute scans (`full_scan=False`): Checks BOTH `sort=startingTime-desc` (newest)
      AND `sort=price-asc` (lowest price).
    """

    source_name = "allegrolokalnie.pl"

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
        full_scan: bool = False,
    ) -> list[Offer]:
        urls_to_fetch: list[str] = []
        if full_scan:
            for page in (1, 2, 3):
                urls_to_fetch.append(
                    f"https://allegrolokalnie.pl/oferty/q/lego%20{set_number}"
                    f"?stan=nowe&sort=price-asc&page={page}"
                )
            urls_to_fetch.append(
                f"https://allegrolokalnie.pl/oferty/q/lego%20{set_number}"
                f"?stan=nowe&sort=startingTime-desc&page=1"
            )
        else:
            urls_to_fetch = [
                f"https://allegrolokalnie.pl/oferty/q/lego%20{set_number}"
                f"?stan=nowe&sort=startingTime-desc&page=1",
                f"https://allegrolokalnie.pl/oferty/q/lego%20{set_number}"
                f"?stan=nowe&sort=price-asc&page=1",
            ]

        offers: list[Offer] = []
        any_200 = False

        for url in urls_to_fetch:
            self._record_url(url)
            status, html, _ = await http_client.get_text(url)
            if status != 200 or not html:
                self._record_failure(f"HTTP {status} on {url}")
                continue
            any_200 = True

            soup = BeautifulSoup(html, "lxml")
            cards = soup.select(
                "article, a.mlc-card, .mlc-itembox, [data-testid='offer-card']"
            )
            for idx, card in enumerate(cards):
                text = card.get_text(" ", strip=True)
                title_el = card.select_one("h2, h3, .mlc-itembox__title")
                title = (
                    title_el.get_text(" ", strip=True) if title_el else text[:120]
                )

                if not contains_set_number(title, set_number):
                    continue

                price_el = card.select_one(
                    ".ml-offer-price__dollars, .mlc-itembox__price, [itemprop='price']"
                )
                if price_el:
                    item_price = parse_price_number(
                        price_el.get_text(" ", strip=True)
                    )
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

                ext_id = (
                    re.sub(r"[^a-zA-Z0-9_-]", "", href_str[-24:]) or f"alok-{idx}"
                )
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

            if full_scan and len(cards) < 15:
                break

        if any_200:
            self.last_error = None
        return offers


class AllegroScraper(BaseScraper):
    """
    Parser for allegro.pl:
    1) Primary mode: Official Allegro REST API (`/offers/listing`) with OAuth2
       Client Credentials when `ALLEGRO_CLIENT_ID` & `ALLEGRO_CLIENT_SECRET` are set.
    2) Web mode: Browser-impersonated listing page (`order=n` newest + `order=p` lowest price).
    3) Anti-DataDome fallback: If Allegro.pl returns 403 DataDome CAPTCHA on a datacenter IP,
       automatically fetches live Allegro.pl offers via `zklockow.pl/lego-{set_number}`!
    """

    source_name = "allegro.pl"

    def __init__(self) -> None:
        super().__init__()
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
        full_scan: bool = False,
    ) -> list[Offer]:
        # 1. Try Official Allegro REST API if configured
        token = await self._get_oauth_token()
        if token:
            api_offers = await self._fetch_via_rest_api(
                set_number, token, msrp_pln, full_scan
            )
            if api_offers:
                return api_offers

        # 2. Try direct web listing pages with "lego {set_number}" (newest order=n & lowest price order=p)
        urls_to_fetch = [
            f"https://allegro.pl/listing?string=lego%20{set_number}&stan=nowe&offerTypeBuyNow=1&order=p&p=1",
            f"https://allegro.pl/listing?string=lego%20{set_number}&stan=nowe&offerTypeBuyNow=1&order=n&p=1",
        ]

        offers: list[Offer] = []
        direct_200 = False
        product_urls_seen: set[str] = set()

        for url in urls_to_fetch:
            self._record_url(url)
            status, html, _ = await http_client.get_text(
                url,
                use_jina_html_on_block=False,
            )
            if status == 200 and html and "datadome" not in html.lower():
                direct_200 = True
                soup = BeautifulSoup(html, "lxml")
                # Also discover Allegro product page links (/produkt/...) to fetch all seller options
                for prod_a in soup.select("a[href*='/produkt/']"):
                    phref = str(prod_a.get("href", "")).split("?")[0].strip()
                    if phref and contains_set_number(phref, set_number):
                        full_p = phref if phref.startswith("http") else f"https://allegro.pl{phref}"
                        product_urls_seen.add(full_p)

                offers.extend(self._parse_allegro_listing_html(soup, set_number, msrp_pln))
            else:
                self._record_failure(f"HTTP {status} (DataDome/WAF) on {url}")
                # If the very first direct Allegro request is blocked by DataDome/WAF, skip the 2nd URL
                if not direct_200 and status in (403, 429):
                    break

        # Open identified Allegro product page(s) to scrape all seller options at the bottom
        for prod_url in list(product_urls_seen)[:2]:
            all_sellers_url = f"{prod_url}?stan=nowe&offerTypeBuyNow=1&order=p"
            self._record_url(all_sellers_url)
            p_status, p_html, _ = await http_client.get_text(
                all_sellers_url,
                use_jina_html_on_block=False,
            )
            if p_status == 200 and p_html and "datadome" not in p_html.lower():
                psoup = BeautifulSoup(p_html, "lxml")
                offers.extend(self._parse_allegro_listing_html(psoup, set_number, msrp_pln))

        # 3. If direct allegro.pl was blocked by DataDome on datacenter IP, use zklockow.pl #oferty-allegro feed
        if not direct_200:
            fallback_url = f"https://zklockow.pl/lego-{set_number}"
            self._record_url(fallback_url)
            fb_status, fb_html, _ = await http_client.get_text(
                fallback_url,
                use_jina_html_on_block=False,
            )
            if fb_status == 200 and fb_html:
                self.last_error = None
                soup = BeautifulSoup(fb_html, "lxml")
                for idx, a_tag in enumerate(soup.select("a.Off[href]")):
                    text = a_tag.get_text(" ", strip=True)
                    href = str(a_tag.get("href", "")).strip()
                    if href.startswith("#"):
                        continue
                    # Pick all Allegro seller offers ('Super Sprzedawcy', 'Konto: Firma', or 'od allegro.pl')
                    is_allegro_row = (
                        "idź do oferty" in text.lower()
                        or "super sprzedawcy" in text.lower()
                        or "konto: firma" in text.lower()
                        or "od allegro.pl" in text.lower()
                    )
                    if not is_allegro_row:
                        continue
                    if not contains_set_number(text, set_number):
                        continue
                    price_matches = re.findall(
                        r"(\d[\d\s]*,\s*\d{2})\s*z[łl]", text, flags=re.I
                    )
                    if not price_matches:
                        continue
                    item_price = parse_price_number(price_matches[0])
                    if not item_price:
                        continue
                    if not is_authentic_and_complete_set(
                        title=text,
                        set_number=set_number,
                        msrp_pln=msrp_pln,
                        item_price_pln=item_price,
                    ):
                        continue

                    # User has Allegro Smart! -> free delivery (0.0 PLN) on Smart-eligible Super Sprzedawcy / Firma offers >= 45 PLN
                    is_smart_eligible = (
                        item_price >= 45.0
                        and (
                            "smart" in text.lower()
                            or "darmowa dostawa" in text.lower()
                            or "super sprzedawcy" in text.lower()
                            or "konto: firma" in text.lower()
                            or "od allegro.pl" in text.lower()
                        )
                    )
                    if is_smart_eligible:
                        delivery_price: Optional[float] = 0.0
                    else:
                        deliv_m = re.search(
                            r"\+\s*(\d[\d\s]*,\s*\d{2})\s*z[łl]\s*dostawa",
                            text,
                            flags=re.I,
                        )
                        delivery_price = (
                            parse_price_number(deliv_m.group(1)) if deliv_m else None
                        )
                    seller_m = re.search(
                        r"\bod\s+(?:Super\s+Sprzedawcy\s+)?([A-Za-z0-9._-]+)",
                        text,
                    )
                    seller = seller_m.group(1) if seller_m else "Seller"
                    full_url = (
                        href
                        if href.startswith("http")
                        else f"https://zklockow.pl{href}"
                    )
                    ext_id = (
                        re.sub(r"[^a-zA-Z0-9_-]", "", f"alg-{seller}-{idx}")
                    )
                    offers.append(
                        Offer(
                            set_number=set_number,
                            title=text.split(" od ")[0].strip()[:120],
                            source=self.source_name,
                            store_name=f"Allegro ({seller} - Smart!)" if is_smart_eligible else f"Allegro ({seller})",
                            url=full_url,
                            external_id=ext_id,
                            item_price_original=item_price,
                            currency_original="PLN",
                            item_price_pln=item_price,
                            delivery_price_pln=delivery_price,
                            delivery_time="1-2 dni robocze (Allegro Smart!)" if is_smart_eligible else "1-2 dni robocze (Allegro)",
                            condition="New (Sealed)",
                        )
                    )
            else:
                # 4. Datacenter IP fallback via Promoklocki decoded Allegro Smart! offer link
                from bot.scrapers.promoklocki import decode_promoklocki_offer_url
                pk_url = f"https://promoklocki.pl/{set_number}"
                self._record_url(pk_url)
                pk_status, pk_html, _ = await http_client.get_text(pk_url)
                if pk_status == 200 and pk_html:
                    self.last_error = None
                    soup = BeautifulSoup(pk_html, "lxml")
                    for idx, row in enumerate(soup.select("table tr, div.row")):
                        row_text = row.get_text(" ", strip=True)
                        if "allegro" not in row_text.lower() or "zł" not in row_text.lower():
                            continue
                        link_el = row.select_one("a[href*='/link/']") or row.select_one("a[href*='/oferta/']")
                        if not link_el:
                            continue
                        decoded_url = decode_promoklocki_offer_url(str(link_el.get("href", "")))
                        if "allegro.pl/oferta/" not in decoded_url.lower():
                            continue
                        price_matches = re.findall(r"(\d[\d\s]*[,.]\d{2})\s*z[łl]", row_text, flags=re.I)
                        if not price_matches:
                            continue
                        item_price = parse_price_number(price_matches[0])
                        if not item_price:
                            continue
                        m_id = re.search(r"/oferta/(\d+)", decoded_url)
                        ext_id = m_id.group(1) if m_id else f"alg-pk-{set_number}"
                        offers.append(
                            Offer(
                                set_number=set_number,
                                title=f"LEGO {set_number} (Allegro Smart!)",
                                source=self.source_name,
                                store_name="Allegro (Smart! Offer)",
                                url=decoded_url,
                                external_id=ext_id,
                                item_price_original=item_price,
                                currency_original="PLN",
                                item_price_pln=item_price,
                                delivery_price_pln=0.0,
                                delivery_time="1-2 dni robocze (Allegro Smart!)",
                                condition="New (Sealed)",
                            )
                        )
                        break

        return offers

    def _parse_allegro_listing_html(
        self,
        soup: BeautifulSoup,
        set_number: str,
        msrp_pln: Optional[float],
    ) -> list[Offer]:
        parsed: list[Offer] = []
        articles = soup.select("article[data-analytics-view-value], article")
        for idx, art in enumerate(articles):
            text = art.get_text(" ", strip=True)
            a_tag = art.select_one("h2 a[href], a[href*='/oferta/']")
            if not a_tag:
                continue
            title = a_tag.get_text(" ", strip=True) or text[:120]
            if not contains_set_number(title, set_number):
                continue

            href = str(a_tag.get("href", "")).strip()
            if not href:
                continue
            full_url = (
                href
                if href.startswith("http")
                else f"https://allegro.pl{href}"
            )

            price_matches = re.findall(
                r"(\d[\d\s]*[,.]\d{2})\s*z[łl]", text, flags=re.I
            )
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

            # Take Allegro Smart! into account (free delivery when marked Smart or Super Sprzedawca >= 45 PLN)
            delivery_price: Optional[float] = None
            if (
                "smart" in text.lower()
                or "darmowa dostawa" in text.lower()
                or (item_price >= 45.0 and "super sprzedawcy" in text.lower())
            ):
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

            ext_id = str(
                art.get("data-analytics-view-value")
                or re.sub(r"[^0-9]", "", href)[-14:]
                or f"alg-{idx}"
            )
            parsed.append(
                Offer(
                    set_number=set_number,
                    title=title,
                    source=self.source_name,
                    store_name="Allegro Seller (Smart!)" if delivery_price == 0.0 else "Allegro Seller",
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
        return parsed

    async def _fetch_via_rest_api(
        self,
        set_number: str,
        token: str,
        msrp_pln: Optional[float],
        full_scan: bool,
    ) -> list[Offer]:
        sorts = ["+withDeliveryPrice", "-startTime"]
        offers: list[Offer] = []
        any_ok = False

        for sort_param in sorts:
            api_url = (
                f"https://api.allegro.pl/offers/listing"
                f"?phrase=LEGO%20{set_number}&sort={sort_param}&limit=60"
            )
            self._record_url(api_url)
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
                        self._record_failure(f"HTTP {resp.status_code} on {api_url}")
                        continue
                    any_ok = True
                    data = resp.json()
            except Exception as exc:
                self._record_failure(f"Exception on {api_url}: {exc}")
                continue

            items_obj = data.get("items", {})
            all_items = (items_obj.get("promoted", []) or []) + (
                items_obj.get("regular", []) or []
            )

            for item in all_items:
                if not isinstance(item, dict):
                    continue
                ext_id = str(item.get("id", ""))
                title = str(item.get("name", "")).strip()
                if not contains_set_number(title, set_number):
                    continue

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

                deliv_obj = (
                    (item.get("delivery", {}) or {}).get("lowestPrice", {}) or {}
                )
                deliv_amt = deliv_obj.get("amount")
                deliv_curr = str(deliv_obj.get("currency", "PLN") or "PLN")
                delivery_pln: Optional[float] = None
                if deliv_amt is not None:
                    delivery_pln = await currency_converter.to_pln(
                        float(deliv_amt), deliv_curr
                    )

                images = item.get("images", []) or []
                img_url = (
                    str(images[0].get("url", ""))
                    if images and isinstance(images[0], dict)
                    else None
                )

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

        if any_ok:
            self.last_error = None
        return offers
