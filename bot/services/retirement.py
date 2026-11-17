from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from bs4 import BeautifulSoup

from bot.config import settings
from bot.services.http_client import http_client

logger = logging.getLogger(__name__)

MONTH_MAP: dict[str, int] = {
    "jan": 1, "january": 1, "styczeń": 1, "stycznia": 1,
    "feb": 2, "february": 2, "luty": 2, "lutego": 2,
    "mar": 3, "march": 3, "marzec": 3, "marca": 3,
    "apr": 4, "april": 4, "kwiecień": 4, "kwietnia": 4,
    "may": 5, "maj": 5, "maja": 5,
    "jun": 6, "june": 6, "czerwiec": 6, "czerwca": 6,
    "jul": 7, "july": 7, "lipiec": 7, "lipca": 7,
    "aug": 8, "august": 8, "sierpień": 8, "sierpnia": 8,
    "sep": 9, "sept": 9, "september": 9, "wrzesień": 9, "września": 9,
    "oct": 10, "october": 10, "październik": 10, "października": 10,
    "nov": 11, "november": 11, "listopad": 11, "listopada": 11,
    "dec": 12, "december": 12, "grudzień": 12, "grudnia": 12,
}

# Ordered list of exact milestones requested by user:
# 1 year / 9m / 8m / 7m / 6m / 5m / 4m / 3m / 2m / 1m / 2 weeks
# Each tuple is: (milestone_key, human_label, min_days_inclusive, max_days_inclusive)
RETIREMENT_MILESTONES: tuple[tuple[str, str, int, int], ...] = (
    ("1_year", "1 year", 345, 372),
    ("9_months", "9 months", 259, 282),
    ("8_months", "8 months", 229, 252),
    ("7_months", "7 months", 198, 221),
    ("6_months", "6 months", 168, 191),
    ("5_months", "5 months", 138, 161),
    ("4_months", "4 months", 107, 130),
    ("3_months", "3 months", 77, 100),
    ("2_months", "2 months", 46, 69),
    ("1_month", "1 month", 22, 38),
    ("2_weeks", "2 weeks", 1, 17),
)


@dataclass
class RetirementInfo:
    set_number: str
    retirement_date: Optional[date]
    source: str
    confidence: str  # "official", "estimated"
    is_already_retired: bool = False
    set_name: Optional[str] = None
    msrp_eur: Optional[float] = None
    image_url: Optional[str] = None


def parse_flexible_date(text: str) -> Optional[date]:
    """
    Parse dates like:
    - '31 Dec 2026' or '2026-12-31'
    - 'December 2026' or 'Jul 2027' (defaults to last day of that month)
    - 'Mid 2027' -> 2027-07-31, 'Late 2027' -> 2027-12-31
    - '2027' -> 2027-12-31
    """
    if not text:
        return None
    clean = text.strip()

    # 1. ISO YYYY-MM-DD
    iso_m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", clean)
    if iso_m:
        try:
            return date(int(iso_m.group(1)), int(iso_m.group(2)), int(iso_m.group(3)))
        except ValueError:
            pass

    # 2. DD Month YYYY (e.g. '31 Dec 2026')
    dmy_m = re.search(r"\b(\d{1,2})\s+([A-Za-zżźćńółęąśŻŹĆĄŚĘŁÓŃ]+)\s+(20\d{2})\b", clean)
    if dmy_m:
        day = int(dmy_m.group(1))
        m_name = dmy_m.group(2).lower()
        year = int(dmy_m.group(3))
        month = MONTH_MAP.get(m_name) or MONTH_MAP.get(m_name[:3])
        if month:
            last_day = calendar.monthrange(year, month)[1]
            return date(year, month, min(day, last_day))

    # 3. Month YYYY (e.g. 'December 2026', 'July 2027')
    my_m = re.search(r"\b([A-Za-zżźćńółęąśŻŹĆĄŚĘŁÓŃ]+)\s+(20\d{2})\b", clean)
    if my_m:
        m_name = my_m.group(1).lower()
        year = int(my_m.group(2))
        if m_name in ("mid", "połowa"):
            return date(year, 7, 31)
        if m_name in ("late", "koniec", "q4"):
            return date(year, 12, 31)
        if m_name in ("early", "początek", "q1"):
            return date(year, 3, 31)
        month = MONTH_MAP.get(m_name) or MONTH_MAP.get(m_name[:3])
        if month:
            last_day = calendar.monthrange(year, month)[1]
            return date(year, month, last_day)

    # 4. Standalone year 2025..2032
    y_m = re.search(r"\b(202[5-9]|203[0-2])\b", clean)
    if y_m:
        return date(int(y_m.group(1)), 12, 31)

    return None


def match_retirement_milestone(
    retirement_date: date,
    today: Optional[date] = None,
) -> Optional[tuple[str, str, int]]:
    """
    Given a retirement_date and today's date, check if `days_remaining` falls inside
    one of the user's exact milestone windows:
      1 year, 9m, 8m, 7m, 6m, 5m, 4m, 3m, 2m, 1m, 2 weeks.
    Returns (milestone_key, human_label, days_remaining) or None.
    """
    ref = today or datetime.utcnow().date()
    days_remaining = (retirement_date - ref).days
    if days_remaining <= 0:
        return None

    for key, label, min_d, max_d in RETIREMENT_MILESTONES:
        if min_d <= days_remaining <= max_d:
            return key, label, days_remaining

    return None


class RetirementService:
    """
    Resolves LEGO set retirement (EOL) dates from multiple sources of truth:
    1) Brickset (Official LEGO ExitDate via API v3 or web page `brickset.com/sets/{set}-1`)
    2) BrickEconomy (`brickeconomy.com/set/{set}-1/`)
    3) Promoklocki (`promoklocki.pl/{set}`)
    """

    async def fetch_retirement_info(self, set_number: str) -> Optional[RetirementInfo]:
        # 1. Try Brickset first (has official LEGO Launch/Exit dates when published)
        bs_info = await self._check_brickset(set_number)
        if bs_info and bs_info.retirement_date:
            return bs_info

        # 2. Try BrickEconomy (tracks both official retirement dates & accurate EOL forecasts)
        be_info = await self._check_brickeconomy(set_number)
        if be_info and be_info.retirement_date:
            return be_info

        # 3. Try Promoklocki fallback
        pk_info = await self._check_promoklocki(set_number)
        if pk_info and pk_info.retirement_date:
            return pk_info

        return bs_info or be_info or pk_info

    async def _check_brickset(self, set_number: str) -> Optional[RetirementInfo]:
        # Check Brickset web page
        url = f"https://brickset.com/sets/{set_number}-1"
        status, html_text, _ = await http_client.get_text(url)
        if status != 200 or not html_text:
            return None

        soup = BeautifulSoup(html_text, "lxml")
        text = soup.get_text(" ", strip=True)

        set_name: Optional[str] = None
        h1 = soup.find("h1")
        if h1:
            raw_h1 = h1.get_text(" ", strip=True)
            set_name = re.sub(rf"^{re.escape(set_number)}\s*[-–:]?\s*", "", raw_h1).strip() or raw_h1

        msrp_eur: Optional[float] = None
        eur_m = re.search(r"€\s*(\d+(?:[.,]\d{1,2})?)", text)
        if eur_m:
            try:
                msrp_eur = float(eur_m.group(1).replace(",", "."))
            except ValueError:
                msrp_eur = None

        img_url = f"https://images.brickset.com/sets/images/{set_number}-1.jpg"

        # Look for "Launch/exit 01 Mar 23 - 31 Dec 26" or "Exit date: 31 Dec 2026"
        m = re.search(
            r"Launch/exit\s+[^-\n]+-\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})",
            text,
            flags=re.I,
        )
        if m:
            raw_exit = m.group(1).strip()
            # Convert 2-digit year like '31 Dec 26' -> '31 Dec 2026'
            raw_exit = re.sub(r"\b(\d{2})$", r"20\1", raw_exit)
            dt = parse_flexible_date(raw_exit)
            if dt:
                return RetirementInfo(
                    set_number=set_number,
                    retirement_date=dt,
                    source="Brickset (Official LEGO Exit Date)",
                    confidence="official",
                    is_already_retired=dt < datetime.utcnow().date(),
                    set_name=set_name,
                    msrp_eur=msrp_eur,
                    image_url=img_url,
                )

        return RetirementInfo(
            set_number=set_number,
            retirement_date=None,
            source="Brickset",
            confidence="official",
            set_name=set_name,
            msrp_eur=msrp_eur,
            image_url=img_url,
        )

    async def _check_brickeconomy(self, set_number: str) -> Optional[RetirementInfo]:
        url = f"https://www.brickeconomy.com/set/{set_number}-1/"
        status, html, _ = await http_client.get_text(url)
        if status != 200 or not html:
            return None

        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ", strip=True)

        # Look for "Retirement" or "Retiring" row in BrickEconomy's set details
        patterns = (
            r"Retirement(?:\s+Date)?\s*[:\-]?\s*((?:Mid|Late|Early|[A-Za-z]{3,9})\s+20\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2})",
            r"Expected\s+retirement\s*[:\-]?\s*((?:Mid|Late|Early|[A-Za-z]{3,9})\s+20\d{2})",
            r"Retired\s+in\s+([A-Za-z]{3,9}\s+20\d{2})",
        )
        for pat in patterns:
            m = re.search(pat, text, flags=re.I)
            if m:
                dt = parse_flexible_date(m.group(1))
                if dt:
                    return RetirementInfo(
                        set_number=set_number,
                        retirement_date=dt,
                        source="BrickEconomy.com",
                        confidence="estimated",
                        is_already_retired=dt < datetime.utcnow().date(),
                    )

        return None

    async def _check_promoklocki(self, set_number: str) -> Optional[RetirementInfo]:
        url = f"https://promoklocki.pl/{set_number}"
        status, html, _ = await http_client.get_text(url)
        if status != 200 or not html:
            return None

        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ", strip=True)
        m = re.search(
            r"(?:Wycofanie|Koniec\s+produkcji)[:\s]*([A-Za-zżźćńółęąś]*\s*20\d{2}(?:-\d{2}-\d{2})?)",
            text,
            flags=re.I,
        )
        if m:
            dt = parse_flexible_date(m.group(1))
            if dt:
                return RetirementInfo(
                    set_number=set_number,
                    retirement_date=dt,
                    source="Promoklocki.pl",
                    confidence="official",
                    is_already_retired=dt < datetime.utcnow().date(),
                )
        return None


retirement_service = RetirementService()
