from __future__ import annotations

import base64
import logging
import re
from datetime import date
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse
from bs4 import BeautifulSoup

from bot.models import Offer, SetMetadata
from bot.scrapers.base import BaseScraper, parse_price_number
from bot.services.filter import (
    contains_set_number,
    is_authentic_and_complete_set,
    is_valid_offer_url,
)
from bot.services.http_client import http_client

logger = logging.getLogger(__name__)


def decode_promoklocki_offer_url(href: str) -> str:
    """
    Decode Promoklocki's `/oferta/<base64>` or `/link/<base64>` links directly
    to the underlying target store / Allegro URL (unwrapping affiliate redirects).
    """
    raw = (href or "").strip()
    m = re.search(r"/(?:oferta|link)/([A-Za-z0-9_+=-]+)", raw)
    if m:
        b64 = m.group(1)
        pad = (4 - len(b64) % 4) % 4
        try:
            decoded = base64.urlsafe_b64decode(b64 + ("=" * pad)).decode("utf-8", errors="ignore")
            if decoded.startswith("http"):
                parsed = urlparse(decoded)
                qs = parse_qs(parsed.query)
                for key in ("url", "ued", "murl", "dl"):
                    if key in qs and qs[key]:
                        inner = unquote(qs[key][0])
                        if inner.startswith("http"):
                            return inner
                m_inline = re.search(r"url\((https?://[^)]+)\)", decoded)
                if m_inline:
                    return unquote(m_inline.group(1))
                return decoded
        except Exception:
            pass
    return raw if raw.startswith("http") else f"https://promoklocki.pl{raw}"


class PromoklockiScraper(BaseScraper):
    """
    Parser for promoklocki.pl/{set_number}:
    - Extracts official LEGO.pl MSRP catalog price ('Cena katalogowa')
    - Extracts set name, official image, and production/retirement years
    - Extracts current active store offers on the set page
    - Extracts embedded historical lowest price points for chart seeding
    """

    source_name = "promoklocki.pl"

    async def fetch_metadata_and_history(
        self, set_number: str
    ) -> tuple[Optional[SetMetadata], list[Offer], list[tuple[str, float, str]]]:
        url = f"https://promoklocki.pl/{set_number}"
        self._record_url(url)
        status, html, _ = await http_client.get_text(url)
        if status != 200 or not html:
            self._record_failure(f"HTTP {status} on {url}")
            return None, [], []

        soup = BeautifulSoup(html, "lxml")
        meta = SetMetadata(set_number=set_number)

        # 1. Extract Set Title
        h1 = soup.find("h1")
        if h1:
            raw_title = h1.get_text(" ", strip=True)
            meta.name = (
                re.sub(
                    rf"^LEGO\s*®?\s*{re.escape(set_number)}\s*[-–:]?\s*",
                    "",
                    raw_title,
                    flags=re.I,
                ).strip()
                or raw_title
            )

        # 2. Extract Image URL
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            meta.image_url = str(og_img["content"]).strip()
        if not meta.image_url:
            img_tag = soup.select_one(
                "img.img-fluid, .product-image img, img[alt*='LEGO']"
            )
            if img_tag and img_tag.get("src"):
                src = str(img_tag["src"])
                meta.image_url = (
                    src if src.startswith("http") else f"https://promoklocki.pl{src}"
                )

        # 3. Extract Official LEGO.pl Catalog Price (Cena katalogowa)
        page_text = soup.get_text(" ", strip=True)
        cat_match = re.search(
            r"(?:Cena\s+katalogowa|Cena\s+LEGO)[:\s]*([\d\s]+[,.]\d{2})\s*z[łl]",
            page_text,
            flags=re.IGNORECASE,
        )
        if cat_match:
            meta.msrp_pln = parse_price_number(cat_match.group(1))

        # 4. Extract Retirement / EOL hint if listed on Promoklocki
        if re.search(r"wycofany|zakończona produkcja", page_text, flags=re.I):
            meta.is_retired = True
        eol_match = re.search(
            r"(?:Wycofanie|Koniec\s+produkcji)[:\s]*(20\d{2})(?:[-/.](\d{1,2}))?",
            page_text,
            flags=re.I,
        )
        if eol_match:
            year = int(eol_match.group(1))
            month = int(eol_match.group(2)) if eol_match.group(2) else 12
            meta.retirement_date = date(year, min(max(month, 1), 12), 28)
            meta.retirement_source = "promoklocki.pl"

        # 5. Parse current active store offers from Promoklocki table/rows
        offers: list[Offer] = []
        offer_rows = soup.select(
            "table tr, .offer-row, .shop-offer, .row.align-items-center, div.row"
        )
        seen_keys: set[str] = set()
        for idx, row in enumerate(offer_rows):
            row_text = row.get_text(" ", strip=True)
            if "zł" not in row_text.lower() or "do sklepu" not in row_text.lower():
                continue
            if len(row_text) > 400:
                continue

            # Prefer the outbound `/link/` or `/oferta/` link over the `/sklep-...` link
            outbound_el = (
                row.select_one("a[href*='/link/']")
                or row.select_one("a[href*='/oferta/']")
                or row.select_one("a[href]")
            )
            if not outbound_el:
                continue
            href = str(outbound_el.get("href", "")).strip()
            if not href or href.startswith("#") or "javascript:" in href:
                continue
            # Skip internal links to OTHER sets on Promoklocki
            if re.search(r"promoklocki\.pl/\d{4,6}", href) and not contains_set_number(
                href, set_number
            ):
                continue

            full_url = decode_promoklocki_offer_url(href)
            if not is_valid_offer_url(full_url):
                continue
            if "oficjalny sklep lego" in row_text.lower():
                continue

            price_matches = re.findall(
                r"(\d[\d\s]*[,.]\d{2})\s*z[łl]", row_text, flags=re.I
            )
            if not price_matches:
                continue
            item_price = parse_price_number(price_matches[0])
            if not item_price:
                continue

            delivery_price: Optional[float] = None
            if re.search(
                r"darmowa\s+dostawa|wysyłka\s+0\s*z[łl]|dostawa\s+gratis|allegro\s+smart",
                row_text,
                flags=re.I,
            ):
                delivery_price = 0.0
            else:
                deliv_cost_m = re.search(
                    r"koszt\s+wysyłki\s*(\d+(?:[,.]\d{1,2})?)\s*z[łl]",
                    row_text,
                    flags=re.I,
                )
                if deliv_cost_m:
                    delivery_price = parse_price_number(deliv_cost_m.group(1))
                elif len(price_matches) >= 2 and "dostaw" in row_text.lower():
                    second_p = parse_price_number(price_matches[1])
                    if second_p and second_p >= item_price:
                        delivery_price = round(second_p - item_price, 2)
                    elif second_p and second_p < 60:
                        delivery_price = second_p

            store_name = "Promoklocki Shop"
            shop_link = row.select_one("a[href*='/sklep-']")
            shop_img = row.select_one("img[alt]")
            if shop_link and shop_link.get_text(strip=True):
                store_name = f"{shop_link.get_text(strip=True)} (Promoklocki)"
            elif shop_img and shop_img.get("alt"):
                store_name = f"{shop_img.get('alt').strip()} (Promoklocki)"
            elif outbound_el.get_text(strip=True):
                txt = outbound_el.get_text(strip=True)
                if len(txt) < 50 and "kup" not in txt.lower() and "do sklepu" not in txt.lower():
                    store_name = f"{txt} (Promoklocki)"

            offer_title = f"LEGO {set_number} {meta.name or ''}".strip()
            if not is_authentic_and_complete_set(
                title=f"{offer_title} {row_text[:120]}",
                set_number=set_number,
                msrp_pln=meta.msrp_pln,
                item_price_pln=item_price,
            ):
                continue

            clean_store = re.sub(r"[^a-z0-9_-]", "", store_name.lower())
            ext_id = f"pk-{set_number}-{clean_store or idx}"
            if ext_id in seen_keys:
                continue
            seen_keys.add(ext_id)
            offers.append(
                Offer(
                    set_number=set_number,
                    title=offer_title,
                    source=self.source_name,
                    store_name=store_name,
                    url=full_url,
                    external_id=ext_id,
                    item_price_original=item_price,
                    currency_original="PLN",
                    item_price_pln=item_price,
                    delivery_price_pln=delivery_price,
                    delivery_time="1-3 dni robocze",
                    condition="New (Sealed)",
                    image_url=meta.image_url,
                )
            )

        # 6. Extract historical lowest price points from embedded scripts
        history_points: list[tuple[str, float, str]] = []
        for script in soup.find_all("script"):
            stext = script.string or ""
            pairs = re.findall(
                r'["\'](20\d{2}-\d{2}-\d{2})["\']\s*[,:]\s*(\d+(?:\.\d{1,2})?)',
                stext,
            )
            for dt_str, p_str in pairs:
                try:
                    pval = float(p_str)
                    if pval > 10:
                        history_points.append((dt_str, pval, "promoklocki.pl"))
                except ValueError:
                    pass

        return meta, offers, history_points

    async def fetch_metadata(self, set_number: str) -> Optional[SetMetadata]:
        meta, _, _ = await self.fetch_metadata_and_history(set_number)
        return meta

    async def fetch_offers(
        self,
        set_number: str,
        msrp_pln: Optional[float] = None,
        full_scan: bool = False,
    ) -> list[Offer]:
        _, offers, _ = await self.fetch_metadata_and_history(set_number)
        return offers
