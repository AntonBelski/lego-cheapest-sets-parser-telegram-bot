from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit


def sanitize_url_for_telegram(url: str) -> str:
    """
    Ensure a URL is strictly RFC-3986 compliant for Telegram HTML `<a href="...">` links.
    Telegram's server-side HTML entity parser silently strips `<a href="...">` tags if the
    URL contains unencoded characters like `{`, `}`, `"`, spaces, `[`, `]`, or `|`.
    """
    raw = (url or "").strip()
    if not raw.startswith(("http://", "https://")):
        return raw
    try:
        parts = urlsplit(raw)
        safe_path = quote(parts.path, safe="/:@!$&'()*+,;=-._~%")
        safe_query = quote(parts.query, safe="?=&/:@!$'()*+,;-._~%")
        safe_fragment = quote(parts.fragment, safe="?=&/:@!$'()*+,;-._~%")
        return urlunsplit((parts.scheme, parts.netloc, safe_path, safe_query, safe_fragment))
    except Exception:
        return (
            raw.replace(" ", "%20")
            .replace('"', "%22")
            .replace("{", "%7B")
            .replace("}", "%7D")
        )


SOURCE_DISPLAY_LABELS: dict[str, str] = {
    "promoklocki.pl": "Promoklocki",
    "olx.pl": "OLX",
    "vinted.pl": "Vinted",
    "allegrolokalnie.pl": "Allegro Lokalnie",
    "allegro.pl": "Allegro",
    "ceneo.pl": "Ceneo",
    "ebay.pl": "eBay",
    "bricklink.com": "BrickLink",
    "brickmerge.de": "Brickmerge",
    "zklockow.pl": "zklockow",
    "smyk.com": "Smyk",
    "empik.com": "Empik",
    "mediaexpert.pl": "MediaExpert",
}


def format_source_label(source: Optional[str]) -> str:
    """
    Return a clean human-readable source name without `.pl`/`.com`/`.de` TLD
    so Telegram never turns `(ebay.pl, 2026-11-17)` into a link to the main site page.
    """
    raw = (source or "").strip()
    if not raw:
        return "Store"
    lower = raw.lower()
    if lower in SOURCE_DISPLAY_LABELS:
        return SOURCE_DISPLAY_LABELS[lower]
    return raw.replace(".", "·")


def format_logged_url_with_browser_link(url: str, set_number: str) -> str:
    """
    When a scraper queries an internal JSON/AJAX API URL (OLX, Vinted, BrickLink),
    append the equivalent browser web page URL with all selected filters & sorting in brackets.
    """
    u = (url or "").strip()
    if "olx.pl/api/v1/offers" in u:
        if "filter_float_price" in u:
            web_url = f"https://www.olx.pl/oferty/q-lego-{set_number}/?search%5Border%5D=filter_float_price:asc&search%5Bfilter_enum_state%5D%5B0%5D=new"
        else:
            web_url = f"https://www.olx.pl/oferty/q-lego-{set_number}/?search%5Border%5D=created_at:desc&search%5Bfilter_enum_state%5D%5B0%5D=new"
        return f"{u} ({web_url})"
    if "vinted.pl/api/v2/catalog/items" in u:
        if "price_low_to_high" in u:
            web_url = f"https://www.vinted.pl/catalog?search_text=lego%20{set_number}&status_ids[]=6&status_ids[]=1&order=price_low_to_high"
        else:
            web_url = f"https://www.vinted.pl/catalog?search_text=lego%20{set_number}&status_ids[]=6&status_ids[]=1&order=newest_first"
        return f"{u} ({web_url})"
    if "bricklink.com/ajax/clone/" in u:
        web_url = f'https://www.bricklink.com/v2/catalog/catalogitem.page?S={set_number}-1#T=S&O={{"cond":"N","ii":0,"ic":0,"iconly":0}}'
        return f"{u} ({web_url})"
    return u


def format_retirement_status_and_countdown(
    retirement_date_str: Optional[str],
    retirement_source: Optional[str] = None,
    today: Optional[date] = None,
) -> str:
    """
    Format retirement status ('Retired: Yes/No') together with retirement date
    and exact years / months / weeks / days remaining before retirement.
    """
    ref = today or datetime.now(timezone.utc).date()
    if not retirement_date_str:
        return "🟢 Active (Not retired | Retirement date not announced yet)"

    try:
        ret_date = date.fromisoformat(retirement_date_str[:10])
    except ValueError:
        return f"🟢 Active (Expected retirement: {retirement_date_str})"

    total_days = (ret_date - ref).days
    src_note = f" [source: {retirement_source}]" if retirement_source else ""

    if total_days < 0:
        return f"🔴 Retired: YES (Retired on {ret_date.isoformat()}, {abs(total_days)} days ago){src_note}"

    if total_days == 0:
        return f"⚠️ Retired: Retiring TODAY ({ret_date.isoformat()}){src_note}"

    # Break down total_days into years, months, weeks, days
    years = total_days // 365
    rem = total_days % 365
    months = rem // 30
    rem = rem % 30
    weeks = rem // 7
    days = rem % 7

    parts: list[str] = []
    if years > 0:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if months > 0:
        parts.append(f"{months} month{'s' if months != 1 else ''}")
    if weeks > 0:
        parts.append(f"{weeks} week{'s' if weeks != 1 else ''}")
    if days > 0 or not parts:
        parts.append(f"{days} day{'s' if days != 1 else ''}")

    countdown_str = ", ".join(parts)
    return (
        f"🟢 Retired: NO — Retires on {ret_date.isoformat()} "
        f"(in {countdown_str} | {total_days} days left){src_note}"
    )


@dataclass
class SetMetadata:
    """Metadata about a LEGO set (MSRP on lego.pl, name, image, retirement info)."""

    set_number: str
    name: str = ""
    msrp_pln: Optional[float] = None  # Official catalog price on lego.pl in PLN
    image_url: Optional[str] = None
    release_year: Optional[int] = None
    retirement_date: Optional[date] = None  # Expected or actual retirement date
    retirement_source: Optional[str] = None
    is_retired: bool = False


@dataclass
class Offer:
    """A single valid LEGO set offer from one of the monitored sites."""

    set_number: str
    title: str
    source: str  # e.g., "olx.pl", "vinted.pl", "promoklocki.pl", "brickmerge.de"
    store_name: str  # Specific store or marketplace seller name
    url: str
    external_id: str  # Listing ID or canonical URL path
    item_price_original: float
    currency_original: str
    item_price_pln: float
    delivery_price_pln: Optional[float] = None  # None = unknown -> "(+delivery)"
    delivery_time: Optional[str] = None
    condition: str = "New (Sealed)"  # "New (Sealed)" or "New (Open Box, Sealed Bags)"
    image_url: Optional[str] = None
    previous_price_pln: Optional[float] = None  # Populated when a listing's price changed
    found_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        self.url = sanitize_url_for_telegram(self.url)

    @property
    def delivery_known(self) -> bool:
        return self.delivery_price_pln is not None

    @property
    def total_price_pln(self) -> float:
        """Final price in PLN including cheapest known delivery."""
        return round(self.item_price_pln + (self.delivery_price_pln or 0.0), 2)

    @property
    def formatted_price(self) -> str:
        """
        Human-friendly final price with explicit delivery status:
        - Unknown delivery: '1250.00 PLN (delivery price unknown)'
        - Free delivery / Smart!: '1250.00 PLN (free delivery)'
        - Known paid delivery: '1264.99 PLN (1250.00 PLN + 14.99 PLN delivery)'
        """
        if self.delivery_price_pln is None:
            base = f"{self.total_price_pln:.2f} PLN (delivery price unknown)"
        elif self.delivery_price_pln == 0:
            base = f"{self.total_price_pln:.2f} PLN (free delivery)"
        else:
            base = (
                f"{self.total_price_pln:.2f} PLN "
                f"({self.item_price_pln:.2f} PLN + {self.delivery_price_pln:.2f} PLN delivery)"
            )
        if (
            self.previous_price_pln is not None
            and abs(self.previous_price_pln - self.total_price_pln) >= 1.0
        ):
            return f"{self.previous_price_pln:.2f} PLN ➔ {base}"
        return base

    def format_log_item_line(self) -> str:
        """
        Format scraped item for run logs:
        <Item name>: <link to the item> (<price in polish zlt or original price and in brackets (~in polish zloty)>)
        """
        if self.currency_original.upper() == "PLN":
            if self.delivery_price_pln is not None and self.delivery_price_pln > 0:
                price_part = (
                    f"{self.total_price_pln:.2f} zł "
                    f"[{self.item_price_pln:.2f} zł + {self.delivery_price_pln:.2f} zł delivery]"
                )
            elif self.delivery_price_pln == 0:
                price_part = f"{self.total_price_pln:.2f} zł [free delivery]"
            else:
                price_part = f"{self.total_price_pln:.2f} zł [delivery unknown]"
        else:
            price_part = (
                f"{self.item_price_original:.2f} {self.currency_original.upper()} "
                f"(~{self.total_price_pln:.2f} zł)"
            )
        return f"{self.title}: {self.url} ({price_part})"

    @property
    def offer_fingerprint(self) -> str:
        """
        Smart deduplication key for a specific listing & price level.
        Ensures the user never receives the same notification twice for the same set
        listing unless its total price drops by at least 1 PLN.
        """
        price_bucket = int(round(self.total_price_pln))
        raw = f"{self.source.lower()}|{self.set_number}|{self.external_id}|{price_bucket}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass
class ScraperSiteReport:
    """Detailed execution report for a single scraper on a single set during a 5-min run."""

    set_number: str
    source: str
    urls_scraped: list[str] = field(default_factory=list)
    success: bool = True
    offers: list[Offer] = field(default_factory=list)
    error_message: Optional[str] = None


@dataclass
class Subscription:
    """User's subscription to a LEGO set with custom [price_from, price_to] range."""

    id: Optional[int]
    chat_id: int
    set_number: str
    price_from: float = 0.0  # Default 0.0 if skipped
    price_to: Optional[float] = None  # None means infinity (∞) if skipped
    set_name: Optional[str] = None
    msrp_pln: Optional[float] = None
    image_url: Optional[str] = None
    retirement_date: Optional[str] = None  # YYYY-MM-DD
    retirement_source: Optional[str] = None
    initial_scan_done: bool = False
    created_at: Optional[str] = None

    def matches_price(self, total_price_pln: float) -> bool:
        """Check if a total price (with delivery) falls within [price_from, price_to]."""
        if total_price_pln < self.price_from:
            return False
        if self.price_to is not None and total_price_pln > self.price_to:
            return False
        return True

    @property
    def formatted_range(self) -> str:
        low = f"{self.price_from:.0f} PLN" if self.price_from > 0 else "0 PLN"
        high = f"{self.price_to:.0f} PLN" if self.price_to is not None else "∞"
        return f"{low} – {high}"


@dataclass
class PriceStats:
    """Historical price statistics for a LEGO set across ALL stored records."""

    set_number: str
    lowest_30d_pln: Optional[float] = None
    lowest_30d_source: Optional[str] = None
    lowest_30d_date: Optional[str] = None
    lowest_all_time_pln: Optional[float] = None
    lowest_all_time_source: Optional[str] = None
    lowest_all_time_date: Optional[str] = None
    daily_lowest_series: list[tuple[str, float]] = field(default_factory=list)
