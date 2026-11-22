from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import pytest

from bot.db import Database
from bot.models import (
    Offer,
    ScraperSiteReport,
    Subscription,
    format_retirement_status_and_countdown,
    sanitize_url_for_telegram,
)
from bot.scrapers.brickmerge import check_brickmerge_shop_ships_to_poland
from bot.scrapers.polish_retailers import MediaExpertScraper
from bot.scrapers.promoklocki import decode_promoklocki_offer_url
from bot.services.chart import render_deal_card_with_chart
from bot.services.currency import CurrencyConverter
from bot.services.filter import (
    classify_condition,
    contains_set_number,
    is_authentic_and_complete_set,
)
from bot.services.monitor import LegoMonitorService
from bot.services.retirement import match_retirement_milestone, parse_flexible_date
from bot.telegram_bot import parse_optional_bound


def test_price_bounds_skip_and_matching() -> None:
    assert parse_optional_bound("", is_min=True) == 0.0
    assert parse_optional_bound("-", is_min=True) == 0.0
    assert parse_optional_bound("skip", is_min=True) == 0.0
    assert parse_optional_bound("", is_min=False) is None
    assert parse_optional_bound("-", is_min=False) is None
    assert parse_optional_bound("inf", is_min=False) is None
    assert parse_optional_bound("1750", is_min=False) == 1750.0

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
    assert contains_set_number("LEGO Ideas 21348 Dungeons & Dragons", "21348")
    assert contains_set_number("LEGO 21348-1 Red Dragon", "21348")
    assert not contains_set_number("LEGO 213489 Other Set", "21348")
    assert not contains_set_number("LEGO Ideas 21365 Papużki", "21348")

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
    assert not is_authentic_and_complete_set(
        "Klocki - Dungeons & Dragons: Opowieść Czerwonego Smoka - jak LEGO 21348",
        set_number="21348",
    )
    assert not is_authentic_and_complete_set(
        "Gablota Drewlandia do zestawu LEGO 21348 - D&D: Opowieść czerwonego smoka",
        set_number="21348",
    )
    assert not is_authentic_and_complete_set(
        "Dungeons & Dragons 21348 – Opowieść o Czerwonym Smoku – odpowiednik LEGO – Nowy",
        set_number="21348",
    )
    assert not is_authentic_and_complete_set(
        "ACRYL PLEXIGLAS TEAK - SHOWCASE - TEK FÜR LEGO® SET 21348 - Dungeons & Dragons",
        set_number="21348",
    )
    assert not is_authentic_and_complete_set(
        "NEU Lego Ideas 21348 Dungeons & Dragons: Red Dragon's Tale - nur Aschenputtel",
        set_number="21348",
    )
    assert not is_authentic_and_complete_set(
        "LMB 2.0 Beleuchtungsset für LEGO The Botanical Garden 21353 geeignet",
        set_number="21353",
    )
    assert not is_authentic_and_complete_set(
        "LEGO® TEC Skianzug Schneeanzug Snowsuit 21348 LWJULIAN 711-894 NEU Gr. 86 /18mon",
        set_number="21348",
    )

    # URL sanitization for Telegram <a href="..."> (no raw '{', '}', or '"')
    raw_bl = 'https://store.bricklink.com/Masleeuw#/shop?o={"invID":"487937970"}'
    safe_bl = sanitize_url_for_telegram(raw_bl)
    assert "{" not in safe_bl and "}" not in safe_bl and '"' not in safe_bl
    assert "%7B%22invID%22:%22487937970%22%7D" in safe_bl

    # Promoklocki base64 offer URL decoding
    decoded_alg = decode_promoklocki_offer_url(
        "/link/aHR0cHM6Ly9jbGsudHJhZGVkb3VibGVyLmNvbS9jbGljaz9wPTI4ODM4NiZhPTIzNDc1NDEmdXJsPWh0dHBzJTNBJTJGJTJGYWxsZWdyby5wbCUyRm9mZXJ0YSUyRjE4NDE1ODA2MjU0"
    )
    assert decoded_alg == "https://allegro.pl/oferta/18415806254"

    # MediaExpert installment price guard ('15,90 zł x 110 rat' vs main '1736,40 zł')
    fake_html = """
    <html><body>
      <div class="offer-box">
        <a href="/zabawki/lego/lego-21348-ideas" title="LEGO 21348 Ideas">LEGO 21348 Ideas</a>
        <span>rata 15,90 zł x 110 rat</span>
        <span class="main-price">1 736,40 zł</span>
      </div>
    </body></html>
    """
    me_offers = MediaExpertScraper()._parse_retail_html(fake_html, "21348", msrp_pln=1549.99)
    assert len(me_offers) == 1
    assert me_offers[0].item_price_pln == 1736.40

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


def test_brickmerge_poland_delivery_filter() -> None:
    # Stores that ship to Poland
    assert check_brickmerge_shop_ships_to_poland("Amazon.de")[0] is True
    assert check_brickmerge_shop_ships_to_poland("amazon (FR)")[0] is True
    assert check_brickmerge_shop_ships_to_poland("Proshop")[0] is True
    assert check_brickmerge_shop_ships_to_poland("JB Spielwaren")[0] is True
    assert check_brickmerge_shop_ships_to_poland("Lucky Bricks")[0] is True

    # Stores that do NOT ship to Poland
    assert check_brickmerge_shop_ships_to_poland("Alza")[0] is False
    assert check_brickmerge_shop_ships_to_poland("Office-Partner")[0] is False
    assert check_brickmerge_shop_ships_to_poland("KiSebA")[0] is False
    assert check_brickmerge_shop_ships_to_poland("kaufland.de")[0] is False
    assert check_brickmerge_shop_ships_to_poland("OTTO")[0] is False


def test_delivery_and_retirement_formatting() -> None:
    offer_unknown_deliv = Offer(
        set_number="21348",
        title="LEGO 21348",
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
    assert "(delivery price unknown)" in offer_unknown_deliv.formatted_price

    offer_paid_deliv = Offer(
        set_number="21348",
        title="LEGO 21348",
        source="ceneo.pl",
        store_name="Proshop",
        url="https://www.ceneo.pl/165836290",
        external_id="c1",
        item_price_original=1500.0,
        currency_original="PLN",
        item_price_pln=1500.0,
        delivery_price_pln=14.99,
    )
    assert offer_paid_deliv.formatted_price == "1514.99 PLN (1500.00 PLN + 14.99 PLN delivery)"

    # Retirement countdown formatting (years, months, weeks, days)
    today = date(2026, 11, 17)
    status_active = format_retirement_status_and_countdown("2027-12-31", "BrickEconomy", today=today)
    assert "Retired: NO" in status_active
    assert "1 year" in status_active
    assert "1 month" in status_active

    status_retired = format_retirement_status_and_countdown("2025-12-31", "Brickset", today=today)
    assert "Retired: YES" in status_retired


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
async def test_db_price_drop_from_outside_range_and_run_logs(tmp_path: Path) -> None:
    db_file = tmp_path / "test_lego.db"
    db = Database(db_file)
    await db.init()

    # User subscribes to set 21348 with range up to 1800 PLN
    sub = await db.upsert_subscription(
        chat_id=999,
        set_number="21348",
        price_from=0.0,
        price_to=1800.0,
        set_name="Dungeons & Dragons: Red Dragon's Tale",
        msrp_pln=1549.99,
    )

    # 1. Listing appears at 2000 PLN (OUTSIDE user's <=1800 PLN range)
    offer_2000 = Offer(
        set_number="21348",
        title="LEGO 21348 D&D",
        source="olx.pl",
        store_name="OLX Seller",
        url="https://olx.pl/d/oferta/21348-id1",
        external_id="olx-dd-1",
        item_price_original=2000.0,
        currency_original="PLN",
        item_price_pln=2000.0,
        delivery_price_pln=0.0,
    )
    await db.record_offers("21348", [offer_2000], [sub])
    assert not sub.matches_price(offer_2000.total_price_pln)

    # 2. Seller lowers price of that same listing from 2000 PLN -> 1500 PLN (NOW INSIDE <=1800 PLN range!)
    offer_1500 = Offer(
        set_number="21348",
        title="LEGO 21348 D&D",
        source="olx.pl",
        store_name="OLX Seller",
        url="https://olx.pl/d/oferta/21348-id1",
        external_id="olx-dd-1",
        item_price_original=1500.0,
        currency_original="PLN",
        item_price_pln=1500.0,
        delivery_price_pln=0.0,
    )
    await db.record_offers("21348", [offer_1500], [sub])

    # Verify previous_price_pln was automatically detected from DB history (2000.0 -> 1500.0)!
    assert offer_1500.previous_price_pln == 2000.0
    assert "2000.00 PLN ➔ 1500.00 PLN" in offer_1500.formatted_price
    assert sub.matches_price(offer_1500.total_price_pln)
    assert await db.should_notify_offer(999, offer_1500) is True
    await db.mark_offer_notified(999, offer_1500)
    # Subsequent check at same 1500 PLN price -> False
    assert await db.should_notify_offer(999, offer_1500) is False

    # 3. Test persistent 5-minute run logs with 1 succeeded site and 1 failed site
    rep_ok = ScraperSiteReport(
        set_number="21348",
        source="ceneo.pl",
        urls_scraped=["https://www.ceneo.pl/;szukaj-lego+21348", "https://www.ceneo.pl/165836290;0280-0.htm"],
        success=True,
        offers=[offer_1500],
    )
    rep_fail = ScraperSiteReport(
        set_number="21348",
        source="vinted.pl",
        urls_scraped=["https://www.vinted.pl/api/v2/catalog/items?search_text=lego%2021348"],
        success=False,
        offers=[],
        error_message="HTTP 403",
    )
    run_id = await db.record_scan_run_logs(
        started_at="2026-11-17 15:00:00 UTC",
        finished_at="2026-11-17 15:00:15 UTC",
        overall_status="FAILED (1/2 site checks failed — valid data saved)",
        sets_scanned=1,
        total_offers_found=1,
        failed_sites_count=1,
        summary_log="Sample log",
        site_reports=[rep_ok, rep_fail],
    )
    assert run_id > 0
    recent_runs = await db.get_recent_scan_runs(limit=1)
    assert len(recent_runs) == 1
    assert recent_runs[0]["overall_status"].startswith("FAILED")
    assert len(recent_runs[0]["site_logs"]) == 2

    # 4. Verify chart generation
    stats = await db.get_price_stats("21348")
    png_bytes = await render_deal_card_with_chart(sub, stats, current_best_pln=1500.0)
    assert png_bytes.startswith(b"\x89PNG")
    conv = CurrencyConverter()
    assert conv.to_pln_sync(100.0, "EUR") > 350.0


def test_caption_html_never_truncates_mid_tag() -> None:
    import re
    from bot.models import PriceStats

    sub = Subscription(
        id=1,
        chat_id=196462642,
        set_number="21348",
        price_from=0.0,
        price_to=1500.0,
        set_name="Ideas - Dungeons & Dragons: Red Dragon's Tale",
        msrp_pln=1549.99,
        retirement_date="2026-07-31",
        retirement_source="Brickset (Official LEGO Exit Date)",
    )
    long_offers = [
        Offer(
            set_number="21348",
            title=f"LEGO 21348 Offer {i}",
            source="bricklink.com",
            store_name=f"Super Long BrickLink Store Name Number {i} (Netherlands)",
            url=f"https://store.bricklink.com/VeryLongStoreUsername{i}#/shop?o=%7B%22invID%22:%2248793797{i}%22%7D",
            external_id=f"bl-{i}",
            item_price_original=300.0 + i,
            currency_original="EUR",
            item_price_pln=1311.44 + i,
            delivery_price_pln=None,
            delivery_time="Ships from Netherlands (3-5 days)",
        )
        for i in range(4)
    ]
    stats = PriceStats(
        set_number="21348",
        lowest_30d_pln=1311.44,
        lowest_30d_source="bricklink.com",
        lowest_30d_date="2026-11-17",
        lowest_all_time_pln=1311.44,
        lowest_all_time_source="bricklink.com",
        lowest_all_time_date="2026-11-17",
    )
    caption = LegoMonitorService.format_deal_notification_caption(
        sub, long_offers[0], long_offers, stats
    )
    assert caption.count("<a ") == caption.count("</a>")
    assert caption.count("<b>") == caption.count("</b>")
    assert caption.count("<i>") == caption.count("</i>")
    visible = re.sub(r"<[^>]+>", "", caption)
    assert len(visible) <= 1024


def test_vinted_fee_eu_filters_concise_line_and_merged_notification() -> None:
    from bot.models import PriceStats
    from bot.scrapers.bricklink import EU_COUNTRIES
    from bot.scrapers.ebay import _is_non_eu_listing
    from bot.scrapers.vinted import apply_vinted_buyer_protection_fee

    # 1. Vinted fee formula (283.06 -> 300.11 PLN)
    assert apply_vinted_buyer_protection_fee(283.06) == 300.11

    # 2. EU-only filters
    assert "germany" in EU_COUNTRIES and "de" in EU_COUNTRIES
    assert "poland" in EU_COUNTRIES and "pl" in EU_COUNTRIES
    assert "usa" not in EU_COUNTRIES and "us" not in EU_COUNTRIES
    assert _is_non_eu_listing("Ships from United States", "USD") is True
    assert _is_non_eu_listing("Ships from Canada", "CAD") is True
    assert _is_non_eu_listing("Versand aus Deutschland", "EUR") is False

    # 3. Concise offer line format with estimated EU delivery highlighted in bold
    bl_offer = Offer(
        set_number="76429",
        title="LEGO 76429 Talking Sorting Hat",
        source="bricklink.com",
        store_name="BrickLink: Brickcr8tor Extras (Germany)",
        url='https://store.bricklink.com/Brickcr8tor#/shop?o={"invID":"542333054"}',
        external_id="542333054",
        item_price_original=64.00,
        currency_original="USD",
        item_price_pln=256.14,
        delivery_price_pln=65.0,
        delivery_is_estimated=True,
    )
    line = LegoMonitorService._format_single_offer_html_line(1, bl_offer)
    assert "<b>BrickLink - 321.14 PLN</b>" in line
    assert "256.14 PLN + <b>65.00 PLN est. EU delivery</b> [64.00 USD]" in line

    # 4. Single merged notification caption (no overflow when few offers fit)
    sub = Subscription(
        id=1,
        chat_id=196462642,
        set_number="76429",
        price_from=250.0,
        price_to=350.0,
        set_name="Talking Sorting Hat",
        msrp_pln=419.99,
        retirement_date="2026-07-31",
    )
    stats = PriceStats(set_number="76429")
    small_offers = [
        Offer(
            set_number="76429",
            title="LEGO 76429",
            source="vinted.pl",
            store_name="Vinted.pl Seller",
            url=f"https://www.vinted.pl/items/{i}",
            external_id=str(i),
            item_price_original=300.11 + i,
            currency_original="PLN",
            item_price_pln=300.11 + i,
            delivery_price_pln=None,
        )
        for i in range(3)
    ]
    cap_small, overflow_small = LegoMonitorService.split_caption_and_overflow_offers(
        sub, small_offers[0], small_offers, stats
    )
    assert overflow_small == []
    assert "New / Price-Dropped Offers:" in cap_small

    # When 20 offers exceed 1024 visible chars, overflow triggers message #2
    many_offers = [
        Offer(
            set_number="76429",
            title="LEGO 76429",
            source="vinted.pl",
            store_name="Vinted.pl Seller",
            url=f"https://www.vinted.pl/items/{i}",
            external_id=str(i),
            item_price_original=300.11 + i,
            currency_original="PLN",
            item_price_pln=300.11 + i,
            delivery_price_pln=None,
        )
        for i in range(20)
    ]
    cap_many, overflow_many = LegoMonitorService.split_caption_and_overflow_offers(
        sub, many_offers[0], many_offers, stats
    )
    assert len(overflow_many) == 20
    import re
    assert len(re.sub(r"<[^>]+>", "", cap_many)) <= 1024


def test_format_per_set_parser_summary() -> None:
    by_set = {
        "21348": [
            ScraperSiteReport(
                set_number="21348",
                source="zklockow.pl",
                urls_scraped=[],
                success=True,
                offers=[],
            ),
            ScraperSiteReport(
                set_number="21348",
                source="promoklocki.pl",
                urls_scraped=[],
                success=False,
                offers=[],
                error_message="HTTP 403",
            ),
        ]
    }
    summary = LegoMonitorService.format_per_set_parser_summary(by_set)
    expected_block = (
        "Set 21348 parsing:\n"
        "Succeeded 1/2 parsers:\n"
        "1) zklockow.pl - PASSED\n"
        "2) promoklocki.pl - FAILED"
    )
    assert expected_block in summary
    assert "ERRORS GROUPED BY TYPE (1 failure(s)):" in summary
    assert "HTTP 403 / WAF Block (1):" in summary
    assert LegoMonitorService.format_per_set_parser_summary({}) == ""


def test_subscriptions_list_keyboard_and_compact_range() -> None:
    from bot.keyboards import main_menu_keyboard, subscriptions_list_keyboard

    subs = [
        Subscription(
            id=1,
            chat_id=123,
            set_number="77092",
            price_from=800.0,
            price_to=1200.0,
            set_name="Great Deku Tree 2-in-1",
        ),
        Subscription(
            id=2,
            chat_id=123,
            set_number="76429",
            price_from=250.0,
            price_to=350.0,
            set_name="Harry Potter - Mówiąca Tiara Przydziału",
        ),
        Subscription(
            id=3,
            chat_id=123,
            set_number="10316",
            price_from=0.0,
            price_to=None,
            set_name="Rivendell",
        ),
    ]
    assert subs[0].formatted_compact_range == "800 – 1200 PLN"
    assert subs[1].formatted_compact_range == "250 – 350 PLN"
    assert subs[2].formatted_compact_range == "0 – ∞ PLN"

    kb = subscriptions_list_keyboard(subs)
    main_rows = main_menu_keyboard().inline_keyboard
    assert len(kb.inline_keyboard) == len(subs) + len(main_rows)
    assert kb.inline_keyboard[0][0].callback_data == "set:view:77092"
    assert "77092" in kb.inline_keyboard[0][0].text
    assert kb.inline_keyboard[1][0].callback_data == "set:view:76429"
    assert kb.inline_keyboard[2][0].callback_data == "set:view:10316"
    # Main menu buttons follow at the bottom
    assert kb.inline_keyboard[3:] == main_rows


def test_error_grouping_by_type() -> None:
    from bot.services.monitor import classify_scraper_error, format_grouped_errors_summary

    assert classify_scraper_error("HTTP 403 on https://promoklocki.pl/10335") == "HTTP 403 / WAF Block"
    assert classify_scraper_error("HTTP 429 via r.jina.ai") == "HTTP 429 / Rate Limit"
    assert classify_scraper_error("HTTP 502 on https://www.ceneo.pl/...") == "HTTP 5xx / Upstream Server Error"
    assert classify_scraper_error("HTTP 0 (DataDome/WAF) on ...") == "HTTP 403 / WAF Block"
    assert classify_scraper_error("HTTP 0 on https://example.com") == "Network / Timeout (HTTP 0)"

    reports = [
        ScraperSiteReport(set_number="10335", source="promoklocki.pl", success=False, error_message="HTTP 403 on https://promoklocki.pl/10335"),
        ScraperSiteReport(set_number="21348", source="promoklocki.pl", success=False, error_message="HTTP 403 on https://promoklocki.pl/21348"),
        ScraperSiteReport(set_number="76429", source="ceneo.pl", success=False, error_message="HTTP 502 on https://www.ceneo.pl/"),
        ScraperSiteReport(set_number="77092", source="vinted.pl", success=False, error_message="HTTP 403 direct / HTTP 429 via r.jina.ai"),
    ]
    grouped = format_grouped_errors_summary(reports)
    assert "ERRORS GROUPED BY TYPE (4 failure(s)):" in grouped
    assert "HTTP 403 / WAF Block (2):" in grouped
    assert "promoklocki.pl: 2 set(s) [10335, 21348]" in grouped
    assert "HTTP 429 / Rate Limit (1):" in grouped
    assert "HTTP 5xx / Upstream Server Error (1):" in grouped


def test_vinted_direct_catalog_html_parser() -> None:
    from bot.scrapers.vinted import VintedScraper

    sample_html = """
    <html><body>
      <a href="/items/1234567890-lego-77092-great-deku-tree?referrer=catalog"
         title="LEGO 77092 Great Deku Tree Nowy fabrycznie zamknięty, Marka: LEGO, Stan: Nowy z metką, 1100.00 zł, 1157.90 zł">Item</a>
      <a href="/items/9876543210-lego-77092-empty-box?referrer=catalog"
         title="LEGO 77092 puste pudełko empty box, Marka: LEGO, Stan: Nowy bez metki, 50.00 zł, 55.40 zł">Box</a>
    </body></html>
    """
    scraper = VintedScraper()
    offers = scraper._parse_vinted_catalog_html(sample_html, "77092", msrp_pln=1299.99, seen_ids=set())
    assert len(offers) == 1
    assert offers[0].external_id == "1234567890"
    assert offers[0].item_price_pln == 1157.90
    assert offers[0].url == "https://www.vinted.pl/items/1234567890-lego-77092-great-deku-tree"


@pytest.mark.asyncio
async def test_http_client_response_cache(monkeypatch) -> None:
    from bot.services.http_client import AntiBlockHttpClient

    client = AntiBlockHttpClient()
    call_count = 0

    async def fake_execute_get(url, headers, proxy, timeout, retry_on_429=False):
        nonlocal call_count
        call_count += 1
        return 200, "<html>ok</html>", {}

    async def no_sleep(domain):
        return None

    monkeypatch.setattr(client, "_execute_get", fake_execute_get)
    monkeypatch.setattr(client, "_throttle_domain", no_sleep)

    s1, t1, _ = await client.get_text("https://promoklocki.pl/21348")
    s2, t2, _ = await client.get_text("https://promoklocki.pl/21348", use_jina_html_on_block=False)
    assert s1 == 200 and t1 == "<html>ok</html>"
    assert s2 == 200 and t2 == "<html>ok</html>"
    assert call_count == 1


@pytest.mark.asyncio
async def test_second_notification_shows_price_change_olx_and_all_sites(tmp_path: Path) -> None:
    import re
    from bot.models import PriceStats, is_unique_listing_url

    db = Database(tmp_path / "price_change_test.db")
    await db.init()

    sub = await db.upsert_subscription(
        chat_id=196462642,
        set_number="77092",
        price_from=800.0,
        price_to=1200.0,
        set_name="Great Deku Tree 2-in-1",
        msrp_pln=1299.99,
    )
    stats = PriceStats(set_number="77092")

    # 1. Exact user-reported OLX scenario: 1113.99 (1100 + 13.99 delivery) -> 1100.00 (delivery unknown)
    olx_first = Offer(
        set_number="77092",
        title="Klocki LEGO 77092",
        source="olx.pl",
        store_name="OLX.pl Seller",
        url="https://www.olx.pl/d/oferta/klocki-lego-77092-CID88-ID1doz6d.html",
        external_id="1040123456",
        item_price_original=1100.0,
        currency_original="PLN",
        item_price_pln=1100.0,
        delivery_price_pln=13.99,
    )
    await db.record_offers("77092", [olx_first], [sub])
    assert await db.should_notify_offer(sub.chat_id, olx_first) is True
    first_caption = LegoMonitorService.format_deal_notification_caption(sub, olx_first, [olx_first], stats)
    assert "Price Change:" not in first_caption
    assert "<s>" not in first_caption
    await db.mark_offer_notified(sub.chat_id, olx_first)

    olx_second = Offer(
        set_number="77092",
        title="Klocki LEGO 77092",
        source="olx.pl",
        store_name="OLX.pl Seller",
        url="https://www.olx.pl/d/oferta/klocki-lego-77092-CID88-ID1doz6d.html",
        external_id="1040123456",
        item_price_original=1100.0,
        currency_original="PLN",
        item_price_pln=1100.0,
        delivery_price_pln=None,
    )
    await db.record_offers("77092", [olx_second], [sub])
    assert await db.should_notify_offer(sub.chat_id, olx_second) is True
    assert olx_second.was_previously_notified is True
    assert olx_second.previous_price_pln == 1113.99
    assert olx_second.price_change_pln == -13.99

    second_caption = LegoMonitorService.format_deal_notification_caption(sub, olx_second, [olx_second], stats)
    assert "Current Price: <s>1113.99 PLN</s> ➔ <b>1100.00 PLN</b>" in second_caption
    assert "📉 Price Change: <b>-13.99 PLN (-1.3%)</b> vs previous alert" in second_caption
    assert "item unchanged at 1100.00 PLN; delivery: 13.99 PLN ➔ unknown" in second_caption
    assert "<b>OLX - <s>1113.99 PLN</s> ➔ 1100.00 PLN</b>" in second_caption
    assert second_caption.count("<s>") == second_caption.count("</s>")
    assert second_caption.count("<b>") == second_caption.count("</b>")
    assert second_caption.count("<a ") == second_caption.count("</a>")
    assert len(re.sub(r"<[^>]+>", "", second_caption)) <= 1024
    await db.mark_offer_notified(sub.chat_id, olx_second)

    # 2. Intermediate price spike (1100 -> 1160 not notified -> 1050 notified):
    #    Second notification MUST compare against last notified 1100.00, NOT intermediate 1160.00!
    olx_spike = Offer(
        set_number="77092",
        title="Klocki LEGO 77092",
        source="olx.pl",
        store_name="OLX.pl Seller",
        url="https://www.olx.pl/d/oferta/klocki-lego-77092-CID88-ID1doz6d.html",
        external_id="1040123456",
        item_price_original=1160.0,
        currency_original="PLN",
        item_price_pln=1160.0,
        delivery_price_pln=None,
    )
    await db.record_offers("77092", [olx_spike], [sub])
    assert await db.should_notify_offer(sub.chat_id, olx_spike) is False

    olx_drop = Offer(
        set_number="77092",
        title="Klocki LEGO 77092",
        source="olx.pl",
        store_name="OLX.pl Seller",
        url="https://www.olx.pl/d/oferta/klocki-lego-77092-CID88-ID1doz6d.html",
        external_id="1040123456",
        item_price_original=1050.0,
        currency_original="PLN",
        item_price_pln=1050.0,
        delivery_price_pln=None,
    )
    await db.record_offers("77092", [olx_drop], [sub])
    assert await db.should_notify_offer(sub.chat_id, olx_drop) is True
    assert olx_drop.previous_price_pln == 1100.0
    drop_caption = LegoMonitorService.format_deal_notification_caption(sub, olx_drop, [olx_drop], stats)
    assert "Current Price: <s>1100.00 PLN</s> ➔ <b>1050.00 PLN</b>" in drop_caption
    assert "📉 Price Change: <b>-50.00 PLN (-4.5%)</b> vs previous alert (was 1100.00 PLN)" in drop_caption

    # 3. Verify identical second-notification price-change behavior across ALL monitored websites
    site_samples = [
        ("vinted.pl", "Vinted.pl Seller", "https://www.vinted.pl/items/987654321-lego-77092", "987654321", None),
        ("allegrolokalnie.pl", "Allegro Lokalnie Seller", "https://allegrolokalnie.pl/oferta/lego-77092-nowy-xyz", "lego-77092-nowy-xyz", 0.0),
        ("allegro.pl", "Allegro (SuperShop - Smart!)", "https://allegro.pl/oferta/18415806254", "18415806254", 0.0),
        ("promoklocki.pl", "BricksBerg (Promoklocki)", "https://bricksberg.pl/lego-77092", "pk-77092-bricksberg", 0.0),
        ("ceneo.pl", "Proshop (via Ceneo)", "https://www.ceneo.pl/Click/Offer/?e=abc123", "offer-123", 14.99),
        ("brickmerge.de", "Amazon.de (via Brickmerge)", "https://www.brickmerge.de/go2/999", "bm-amazonde-77092", 0.0),
        ("zklockow.pl", "mediaexpert.pl (via zklockow)", "https://zklockow.pl/przekierowanie/77092-me", "zk-77092-mediaexpertplviazklockow", 0.0),
        ("smyk.com", "Smyk.com", "https://www.smyk.com/p/lego-77092-i999", "smyk-77092", 0.0),
        ("empik.com", "Empik.com", "https://www.empik.com/lego-77092,p123,zabawki-p", "empik-77092", 0.0),
        ("mediaexpert.pl", "MediaExpert.pl", "https://www.mediaexpert.pl/zabawki/lego-77092", "me-77092", 0.0),
        ("ebay.pl", "eBay Seller (ships to PL)", "https://www.ebay.pl/itm/3355779911", "3355779911", 65.0),
        ("bricklink.com", "BrickLink: EuroBricks (Germany)", "https://store.bricklink.com/EuroBricks#/shop?o=%7B%22invID%22:%22555111%22%7D", "555111", 65.0),
    ]
    batch_dropped: list[Offer] = []
    for source, store, url, ext_id, deliv in site_samples:
        off_v1 = Offer(
            set_number="77092",
            title="LEGO 77092 Great Deku Tree",
            source=source,
            store_name=store,
            url=url,
            external_id=ext_id,
            item_price_original=1120.0,
            currency_original="PLN",
            item_price_pln=1120.0,
            delivery_price_pln=deliv,
        )
        await db.record_offers("77092", [off_v1], [sub])
        assert await db.should_notify_offer(sub.chat_id, off_v1) is True
        await db.mark_offer_notified(sub.chat_id, off_v1)

        off_v2 = Offer(
            set_number="77092",
            title="LEGO 77092 Great Deku Tree",
            source=source,
            store_name=store,
            url=url,
            external_id=ext_id,
            item_price_original=1020.0,
            currency_original="PLN",
            item_price_pln=1020.0,
            delivery_price_pln=deliv,
        )
        await db.record_offers("77092", [off_v2], [sub])
        assert await db.should_notify_offer(sub.chat_id, off_v2) is True, f"Failed for {source}"
        assert off_v2.was_previously_notified is True
        assert off_v2.previous_price_pln == round(1120.0 + (deliv or 0.0), 2)
        assert off_v2.price_change_pln == -100.0
        cap_v2 = LegoMonitorService.format_deal_notification_caption(sub, off_v2, [off_v2], stats)
        assert "📉 Price Change: <b>-100.00 PLN" in cap_v2
        assert f"<s>{off_v2.previous_price_pln:.2f} PLN</s> ➔ <b>{off_v2.total_price_pln:.2f} PLN</b>" in cap_v2
        line_v2 = LegoMonitorService._format_single_offer_html_line(1, off_v2)
        assert f"<s>{off_v2.previous_price_pln:.2f}</s> ➔ {off_v2.total_price_pln:.2f} PLN" in line_v2
        assert "<b>-100.00 PLN /" in line_v2
        batch_dropped.append(off_v2)

    # Multi-offer batch message also renders every offer's price change
    batch_msg = LegoMonitorService.format_full_offers_batch_message(sub, batch_dropped)
    assert batch_msg.count("<s>") == len(batch_dropped)
    assert batch_msg.count("</s>") == len(batch_dropped)

    # 4. URL fallback matching when external_id changes for the same unique link
    vinted_changed_ext_id = Offer(
        set_number="77092",
        title="LEGO 77092 Great Deku Tree",
        source="vinted.pl",
        store_name="Vinted.pl Seller",
        url="https://www.vinted.pl/items/987654321-lego-77092",
        external_id="renamed-ext-id-987654321",
        item_price_original=990.0,
        currency_original="PLN",
        item_price_pln=990.0,
        delivery_price_pln=None,
    )
    assert await db.should_notify_offer(sub.chat_id, vinted_changed_ext_id) is True
    assert vinted_changed_ext_id.was_previously_notified is True
    assert vinted_changed_ext_id.previous_price_pln == 1120.0

    # Shared catalog URLs must NOT trigger URL-fallback collisions across different stores
    assert is_unique_listing_url("https://www.ceneo.pl/165836290;0280-0.htm") is False
    assert is_unique_listing_url("https://www.brickmerge.de/77092-1") is False
    assert is_unique_listing_url("https://www.olx.pl/d/oferta/klocki-lego-77092-CID88-ID1doz6d.html") is True


@pytest.mark.asyncio
async def test_multi_subscriber_price_change_isolation(tmp_path: Path, monkeypatch) -> None:
    from bot.models import PriceStats

    db = Database(tmp_path / "multi_sub.db")
    await db.init()

    sub_a = await db.upsert_subscription(chat_id=101, set_number="77092", price_from=0.0, price_to=1200.0, set_name="Great Deku Tree")
    sub_b = await db.upsert_subscription(chat_id=202, set_number="77092", price_from=0.0, price_to=1200.0, set_name="Great Deku Tree")

    # Subscriber A was previously notified at 1150 PLN; Subscriber B has never been notified
    old_offer = Offer(
        set_number="77092",
        title="LEGO 77092",
        source="vinted.pl",
        store_name="Vinted.pl Seller",
        url="https://www.vinted.pl/items/555555-lego-77092",
        external_id="555555",
        item_price_original=1150.0,
        currency_original="PLN",
        item_price_pln=1150.0,
        delivery_price_pln=None,
    )
    await db.mark_offer_notified(sub_a.chat_id, old_offer)

    sent_messages: dict[int, str] = {}

    class FakeBot:
        async def send_message(self, chat_id: int, text: str, **kwargs):
            sent_messages[chat_id] = text

        async def send_photo(self, chat_id: int, caption: str, **kwargs):
            sent_messages[chat_id] = caption

    async def fake_fetch_img(*args, **kwargs):
        return None

    import bot.services.monitor as monitor_mod
    monkeypatch.setattr(monitor_mod, "fetch_set_image_bytes", fake_fetch_img)

    monitor = LegoMonitorService(db=db, bot=FakeBot())  # type: ignore
    new_offer = Offer(
        set_number="77092",
        title="LEGO 77092",
        source="vinted.pl",
        store_name="Vinted.pl Seller",
        url="https://www.vinted.pl/items/555555-lego-77092",
        external_id="555555",
        item_price_original=1050.0,
        currency_original="PLN",
        item_price_pln=1050.0,
        delivery_price_pln=None,
    )
    stats = PriceStats(set_number="77092")
    await monitor._evaluate_and_notify_subscriber(sub_a, [new_offer], stats)
    await monitor._evaluate_and_notify_subscriber(sub_b, [new_offer], stats)

    # Subscriber A sees the price change from their previous alert (1150 -> 1050)
    assert "<s>1150.00 PLN</s> ➔ <b>1050.00 PLN</b>" in sent_messages[101]
    assert "vs previous alert" in sent_messages[101]
    # Subscriber B receives a clean first-time notification without inheriting A's alert state
    assert "vs previous alert" not in sent_messages[202]
    assert "<s>1150.00 PLN</s>" not in sent_messages[202]
