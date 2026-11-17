from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import pytest

from bot.db import Database
from bot.models import Offer, Subscription
from bot.services.chart import render_deal_card_with_chart
from bot.services.currency import CurrencyConverter
from bot.services.filter import (
    classify_condition,
    contains_set_number,
    is_authentic_and_complete_set,
)
from bot.services.retirement import match_retirement_milestone, parse_flexible_date
from bot.telegram_bot import parse_optional_bound


def test_price_bounds_skip_and_matching() -> None:
    # Skipping price_from -> 0.0; skipping price_to -> None (infinity)
    assert parse_optional_bound("", is_min=True) == 0.0
    assert parse_optional_bound("-", is_min=True) == 0.0
    assert parse_optional_bound("skip", is_min=True) == 0.0
    assert parse_optional_bound("", is_min=False) is None
    assert parse_optional_bound("-", is_min=False) is None
    assert parse_optional_bound("inf", is_min=False) is None
    assert parse_optional_bound("1750", is_min=False) == 1750.0

    # Test Subscription matching
    sub_open = Subscription(id=1, chat_id=123, set_number="10316", price_from=0.0, price_to=None)
    assert sub_open.matches_price(100.0)
    assert sub_open.matches_price(99999.0)
    assert sub_open.formatted_range == "0 PLN – ∞"

    sub_max_only = Subscription(id=2, chat_id=123, set_number="10316", price_from=0.0, price_to=1750.0)
    assert sub_max_only.matches_price(1699.99)
    assert not sub_max_only.matches_price(1751.00)
    assert sub_max_only.formatted_range == "0 PLN – 1750 PLN"

    sub_bounded = Subscription(id=3, chat_id=123, set_number="10316", price_from=1200.0, price_to=1750.0)
    assert not sub_bounded.matches_price(1150.0)
    assert sub_bounded.matches_price(1500.0)


def test_authenticity_and_condition_filtering() -> None:
    # Exact set number check
    assert contains_set_number("LEGO Icons 10316 Władca Pierścieni: Rivendell", "10316")
    assert contains_set_number("LEGO 10316-1 Rivendell", "10316")
    assert not contains_set_number("LEGO 103165 Some Other Number", "10316")

    # Original LEGO vs Replicas / Partial sets
    assert is_authentic_and_complete_set(
        "LEGO 10316 Rivendell Nowy Plomby", set_number="10316", msrp_pln=2149.99, item_price_pln=1650.0
    )
    assert not is_authentic_and_complete_set(
        "Klocki kompatybilne z LEGO 10316 Lepin Rivendell", set_number="10316"
    )
    assert not is_authentic_and_complete_set(
        "LEGO 10316 Rivendell - Puste pudełko + instrukcja", set_number="10316"
    )
    assert not is_authentic_and_complete_set(
        "LEGO 10316 Rivendell bez figurek (same klocki)", set_number="10316"
    )
    assert not is_authentic_and_complete_set(
        "Oświetlenie LED do LEGO 10316 Briksmax", set_number="10316"
    )

    # Condition filter: New Sealed vs Open Box Sealed Bags vs Used
    assert classify_condition("LEGO 10316 Nowy fabrycznie zamknięty MISB") == "New (Sealed)"
    assert (
        classify_condition("LEGO 10316 Otwarte pudełko, wszystkie woreczki fabrycznie zamknięte!")
        == "New (Open Box, Sealed Bags)"
    )
    assert (
        classify_condition("LEGO 10316 Open box, all bags sealed")
        == "New (Open Box, Sealed Bags)"
    )
    assert classify_condition("LEGO 10316 Używany, raz złożony, kompletny") is None


def test_delivery_price_formatting() -> None:
    offer_unknown_deliv = Offer(
        set_number="10316",
        title="LEGO 10316",
        source="vinted.pl",
        store_name="Vinted Seller",
        url="https://vinted.pl/items/1",
        external_id="v1",
        item_price_original=1500.0,
        currency_original="PLN",
        item_price_pln=1500.0,
        delivery_price_pln=None,
    )
    assert offer_unknown_deliv.total_price_pln == 1500.0
    assert "(+delivery)" in offer_unknown_deliv.formatted_price

    offer_with_deliv = Offer(
        set_number="10316",
        title="LEGO 10316",
        source="olx.pl",
        store_name="OLX Seller",
        url="https://olx.pl/d/1",
        external_id="o1",
        item_price_original=1500.0,
        currency_original="PLN",
        item_price_pln=1500.0,
        delivery_price_pln=13.99,
    )
    assert offer_with_deliv.total_price_pln == 1513.99
    assert "1513.99 PLN" in offer_with_deliv.formatted_price


def test_retirement_milestones() -> None:
    today = date(2026, 11, 16)
    assert match_retirement_milestone(today + timedelta(days=365), today=today)[0] == "1_year"  # type: ignore
    assert match_retirement_milestone(today + timedelta(days=274), today=today)[0] == "9_months"  # type: ignore
    assert match_retirement_milestone(today + timedelta(days=243), today=today)[0] == "8_months"  # type: ignore
    assert match_retirement_milestone(today + timedelta(days=213), today=today)[0] == "7_months"  # type: ignore
    assert match_retirement_milestone(today + timedelta(days=182), today=today)[0] == "6_months"  # type: ignore
    assert match_retirement_milestone(today + timedelta(days=152), today=today)[0] == "5_months"  # type: ignore
    assert match_retirement_milestone(today + timedelta(days=122), today=today)[0] == "4_months"  # type: ignore
    assert match_retirement_milestone(today + timedelta(days=91), today=today)[0] == "3_months"  # type: ignore
    assert match_retirement_milestone(today + timedelta(days=61), today=today)[0] == "2_months"  # type: ignore
    assert match_retirement_milestone(today + timedelta(days=30), today=today)[0] == "1_month"  # type: ignore
    assert match_retirement_milestone(today + timedelta(days=14), today=today)[0] == "2_weeks"  # type: ignore
    assert match_retirement_milestone(today + timedelta(days=315), today=today) is None

    assert parse_flexible_date("December 2026") == date(2026, 12, 31)
    assert parse_flexible_date("31 Dec 2027") == date(2027, 12, 31)


@pytest.mark.asyncio
async def test_db_all_prices_storage_deduplication_and_chart(tmp_path: Path) -> None:
    db_file = tmp_path / "test_lego.db"
    db = Database(db_file)
    await db.init()

    sub = await db.upsert_subscription(
        chat_id=999,
        set_number="10316",
        price_from=0.0,
        price_to=1700.0,
        set_name="The Lord of the Rings: Rivendell",
        msrp_pln=2149.99,
    )

    # Seed historical prices (including an all-time low 45 days ago and 30d low 10 days ago)
    today = date.today()
    d_45 = (today - timedelta(days=45)).isoformat()
    d_10 = (today - timedelta(days=10)).isoformat()
    await db.seed_historical_daily_prices(
        "10316",
        [
            (d_45, 1490.00, "brickmerge.de"),
            (d_10, 1580.00, "promoklocki.pl"),
        ],
    )

    # Record 2 live offers: one OUTSIDE user's price range (1899 PLN) and one INSIDE (1620 PLN)
    offer_outside = Offer(
        set_number="10316",
        title="LEGO 10316 Rivendell",
        source="ceneo.pl",
        store_name="Ceneo Shop",
        url="https://ceneo.pl/1",
        external_id="cen-1",
        item_price_original=1899.0,
        currency_original="PLN",
        item_price_pln=1899.0,
        delivery_price_pln=0.0,
    )
    offer_inside = Offer(
        set_number="10316",
        title="LEGO 10316 Rivendell Otwarte pudełko woreczki zamknięte",
        source="olx.pl",
        store_name="OLX Seller",
        url="https://olx.pl/1",
        external_id="olx-1",
        item_price_original=1606.01,
        currency_original="PLN",
        item_price_pln=1606.01,
        delivery_price_pln=13.99,
        condition="New (Open Box, Sealed Bags)",
    )

    inserted = await db.record_offers("10316", [offer_outside, offer_inside], [sub])
    assert inserted == 2

    # Check 30-day low vs All-Time low across ALL stored records
    stats = await db.get_price_stats("10316")
    assert stats.lowest_30d_pln == 1580.00
    assert stats.lowest_all_time_pln == 1490.00
    assert len(stats.daily_lowest_series) == 3

    # Verify smart deduplication cache:
    # 1st check -> True (should notify)
    assert await db.should_notify_offer(999, offer_inside) is True
    await db.mark_offer_notified(999, offer_inside)
    # 2nd check for same offer at same price -> False (never notify twice!)
    assert await db.should_notify_offer(999, offer_inside) is False

    # If seller drops price by 20 PLN -> True (notify price drop!)
    offer_dropped = Offer(
        set_number="10316",
        title="LEGO 10316 Rivendell",
        source="olx.pl",
        store_name="OLX Seller",
        url="https://olx.pl/1",
        external_id="olx-1",
        item_price_original=1586.01,
        currency_original="PLN",
        item_price_pln=1586.01,
        delivery_price_pln=13.99,
    )
    assert await db.should_notify_offer(999, offer_dropped) is True

    # Test composite chart PNG generation
    png_bytes = await render_deal_card_with_chart(sub, stats, current_best_pln=1620.0)
    assert png_bytes.startswith(b"\x89PNG")
    assert len(png_bytes) > 5000

    # Test currency conversion fallback
    conv = CurrencyConverter()
    pln_val = conv.to_pln_sync(100.0, "EUR")
    assert pln_val > 350.0
