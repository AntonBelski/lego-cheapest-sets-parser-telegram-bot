from __future__ import annotations

import re
from typing import Optional

# Terms that indicate a replica / fake / knock-off brand instead of original LEGO
REPLICA_KEYWORDS: tuple[str, ...] = (
    "lepin",
    "mould king",
    "mouldking",
    "cada ",
    "cobi ",
    "mega bloks",
    "megabloks",
    "sluban",
    "qman",
    "xingbao",
    "bela ",
    "decool",
    "sy block",
    "sembo",
    "wange",
    "kompatybilne z lego",
    "kompatybilny z lego",
    "compatible with lego",
    "zamiennik lego",
    "klocki typu lego",
    "klocki a'la lego",
    "klocki ala lego",
    "nie lego",
    "not lego",
    "replika",
    "podróbka",
    "podrobka",
    "jak lego",
    "jak klocki lego",
    "w stylu lego",
    "na wzór lego",
    "wzór lego",
    "nieoryginalne",
    "klon lego",
    "custom moc",
    "aliexpress",
)

# Terms that indicate the listing is NOT a complete LEGO set (e.g. empty box, instructions, LED kit, minifigs excluded)
PARTIAL_OR_ACCESSORY_KEYWORDS: tuple[str, ...] = (
    "puste pudełko",
    "samo pudełko",
    "tylko pudełko",
    "empty box",
    "box only",
    "sama instrukcja",
    "tylko instrukcja",
    "instrukcja obsługi",
    "instructions only",
    "instruction only",
    "manual only",
    "bez figurek",
    "brak figurek",
    "bez minifigurek",
    "brak minifigurek",
    "no minifigures",
    "no minifigs",
    "without minifigures",
    "same figurki",
    "tylko figurki",
    "minifigurki z zestawu",
    "minifigurka z zestawu",
    "oświetlenie led",
    "zestaw oświetlenia",
    "led light kit",
    "light kit only",
    "briksmax",
    "lightailing",
    "gablotka",
    "gablota akrylowa",
    "display case",
    "acrylic case",
    "uchwyt ścienny",
    "wall mount",
    "naklejki do",
    "arkusz naklejek",
    "sticker sheet",
    "woreczek nr",
    "woreczki nr",
    "części z zestawu",
    "incomplete",
    "niekompletny",
    "brakuje",
)

# Terms indicating Open Box with Sealed Bags (explicitly allowed by user!)
OPEN_BOX_SEALED_BAGS_PATTERNS: tuple[str, ...] = (
    "woreczki zamknięte",
    "woreczki fabrycznie zamknięte",
    "worki zamknięte",
    "worki fabrycznie zamknięte",
    "torebki zamknięte",
    "woreczki nierozpakowane",
    "woreczki zaplombowane",
    "sealed bags",
    "bags sealed",
    "all bags sealed",
    "factory sealed bags",
    "unopened bags",
    "otwarte pudełko, woreczki",
    "pudełko otwarte, woreczki",
    "open box, sealed",
    "open box sealed",
)

# Terms indicating Used / Pre-built condition (rejected unless open-box with sealed bags)
USED_KEYWORDS: tuple[str, ...] = (
    "używany",
    "używane",
    "używana",
    "złożony",
    "raz złożony",
    "poskładany",
    "zbudowany",
    "rozłożony",
    "used",
    "pre-owned",
    "second hand",
    "built once",
    "already built",
    "stan dobry",
    "stan bardzo dobry",
)


def contains_set_number(text: str, set_number: str) -> bool:
    """
    Ensure the exact LEGO set number appears as a distinct number in the title/URL
    (e.g. '10316' matches 'LEGO 10316' or '10316-1', but NOT '103165' or '210316').
    """
    esc = re.escape(str(set_number).strip())
    pattern = rf"(?<!\d){esc}(?:-\d)?(?!\d)"
    return bool(re.search(pattern, text or "", flags=re.IGNORECASE))


def is_authentic_and_complete_set(
    title: str,
    description: str = "",
    set_number: str = "",
    msrp_pln: Optional[float] = None,
    item_price_pln: Optional[float] = None,
) -> bool:
    """
    Verify that the offer is:
    1) For the requested LEGO set number
    2) An original LEGO set (not a replica/knock-off)
    3) A full set (not box-only, instructions-only, LED kit, or missing minifigures)
    4) Not suspiciously priced like a single minifigure/manual (< 15% of MSRP when MSRP is known)
    """
    combined = f"{title} {description}".lower()

    if set_number and not contains_set_number(combined, set_number):
        return False

    for kw in REPLICA_KEYWORDS:
        if kw in combined:
            return False

    for kw in PARTIAL_OR_ACCESSORY_KEYWORDS:
        if kw in combined:
            return False

    # Sanity check against tiny accessories/manuals slipping through when MSRP is known
    if msrp_pln and msrp_pln > 100 and item_price_pln is not None:
        if item_price_pln < (msrp_pln * 0.18):
            return False

    return True


def classify_condition(
    title: str,
    description: str = "",
    raw_condition: str = "",
) -> Optional[str]:
    """
    Classify the item condition according to the user's rules:
    - Returns "New (Sealed)" for brand new factory-sealed sets.
    - Returns "New (Open Box, Sealed Bags)" if the box was opened/damaged but bags are sealed.
    - Returns None if the set is used/built.
    """
    combined = f"{title} {description} {raw_condition}".lower()

    # Check if it's explicitly Open Box with Sealed Bags first
    for pat in OPEN_BOX_SEALED_BAGS_PATTERNS:
        if pat in combined:
            return "New (Open Box, Sealed Bags)"

    # Also detect combinations like ("otwarte pudełko" or "open box") AND ("woreczki" or "bags" + "zamknięte"/"nowe"/"sealed")
    has_open_box = any(
        p in combined
        for p in ("otwarte pudełko", "pudełko otwarte", "open box", "opened box", "uszkodzone pudełko")
    )
    has_sealed_bags = any(
        p in combined
        for p in (
            "zamknięte",
            "fabrycznie",
            "nierozpakowane",
            "sealed",
            "unopened",
            "nowe woreczki",
        )
    )
    if has_open_box and has_sealed_bags:
        return "New (Open Box, Sealed Bags)"

    # Reject if used keywords appear (and not overridden by sealed bags above)
    for kw in USED_KEYWORDS:
        if kw in combined:
            return None

    # Check raw marketplace condition tags
    raw_lower = (raw_condition or "").strip().lower()
    if raw_lower in (
        "used",
        "używany",
        "używane",
        "zadowalający",
        "dobry",
        "bardzo dobry",
        "good",
        "very_good",
        "satisfactory",
        "3000",  # eBay Used condition ID
    ):
        return None

    if raw_lower in ("1500", "new other", "new_without_tags", "nowy bez metki"):
        return "New (Open Box, Sealed Bags)"

    return "New (Sealed)"
