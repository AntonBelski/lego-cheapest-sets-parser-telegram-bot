from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot

from bot.config import settings
from bot.db import Database
from bot.services.monitor import LegoMonitorService
from bot.telegram_bot import build_dispatcher, parse_optional_bound

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("bot.main")


async def seed_default_subscriptions(db: Database, monitor: LegoMonitorService) -> None:
    """
    If DEFAULT_SUBSCRIPTIONS is set in .env / GitHub Secrets (e.g. '10316:-:1750,75192:2200:2900')
    and TELEGRAM_CHAT_ID is provided, ensure they are present in the database.
    """
    if not settings.default_subscriptions or not settings.telegram_chat_id:
        return

    existing = {
        s.set_number for s in await db.list_subscriptions(chat_id=settings.telegram_chat_id)
    }
    for entry in settings.default_subscriptions.split(","):
        parts = [p.strip() for p in entry.strip().split(":")]
        if not parts or not parts[0].isdigit():
            continue
        set_num = parts[0]
        if set_num in existing:
            continue
        p_from = parse_optional_bound(parts[1], is_min=True) if len(parts) >= 2 else 0.0
        p_to = parse_optional_bound(parts[2], is_min=False) if len(parts) >= 3 else None
        await db.upsert_subscription(
            chat_id=settings.telegram_chat_id,
            set_number=set_num,
            price_from=p_from or 0.0,
            price_to=p_to,
        )
        await monitor.enrich_set_on_subscribe(set_num)
        logger.info("Seeded default subscription for LEGO %s (%s - %s)", set_num, p_from, p_to)


async def drain_pending_telegram_updates(bot: Bot, dp) -> int:
    """
    Process any queued Telegram messages/callbacks when running in one-shot GitHub Actions
    cron mode (`--once`), so interactive commands (/add, /list, /remove, /chart) work
    even without a 24/7 daemon!
    """
    processed = 0
    offset: int | None = None
    try:
        while True:
            updates = await bot.get_updates(offset=offset, timeout=1, limit=50)
            if not updates:
                break
            for upd in updates:
                offset = upd.update_id + 1
                try:
                    await dp.feed_update(bot, upd)
                    processed += 1
                except Exception as upd_exc:
                    logger.warning(
                        "Failed handling update_id=%s in one-shot mode: %s",
                        upd.update_id,
                        upd_exc,
                    )
        if offset is not None:
            # Always acknowledge last update_id so updates aren't re-processed next cron run
            await bot.get_updates(offset=offset, timeout=1, limit=1)
    except Exception as exc:
        logger.warning("Error draining Telegram updates in one-shot mode: %s", exc)
    return processed


async def run_once(check_retirement: bool = False) -> None:
    """Execute a single scan cycle (ideal for GitHub Actions cron or CLI testing)."""
    db = Database(settings.database_path)
    await db.init()

    bot: Bot | None = None
    if settings.telegram_bot_token and settings.telegram_bot_token != "your_telegram_bot_token_here":
        bot = Bot(token=settings.telegram_bot_token)

    monitor = LegoMonitorService(db=db, bot=bot)
    await seed_default_subscriptions(db, monitor)

    try:
        if bot:
            dp = build_dispatcher(db, monitor)
            handled = await drain_pending_telegram_updates(bot, dp)
            if handled:
                logger.info("Processed %d queued Telegram commands in one-shot mode.", handled)

        logger.info("Running price scan for all subscribed LEGO sets...")
        results = await monitor.scan_all_subscribed_sets()
        for set_num, offers in results.items():
            best = offers[0].formatted_price if offers else "none"
            logger.info("Set %s: %d offers found (cheapest: %s)", set_num, len(offers), best)

        if check_retirement:
            logger.info("Running retirement date check across Brickset / BrickEconomy / Promoklocki...")
            await monitor.check_all_retirement_dates()
    finally:
        if bot:
            await bot.session.close()


async def run_daemon() -> None:
    """Run the 24/7 Telegram bot + 5-minute price scheduler + 5-day retirement scheduler."""
    if not settings.telegram_bot_token or settings.telegram_bot_token == "your_telegram_bot_token_here":
        logger.error("TELEGRAM_BOT_TOKEN is not set in .env! Please configure .env first.")
        sys.exit(1)

    db = Database(settings.database_path)
    await db.init()

    bot = Bot(token=settings.telegram_bot_token)
    monitor = LegoMonitorService(db=db, bot=bot)
    await seed_default_subscriptions(db, monitor)

    dp = build_dispatcher(db, monitor)

    scheduler = AsyncIOScheduler()
    # 1. Every 5 minutes: check prices across all 13 Polish & EU sites
    scheduler.add_job(
        monitor.scan_all_subscribed_sets,
        trigger="interval",
        minutes=settings.price_check_interval_minutes,
        id="price_scan_job",
        max_instances=1,
        coalesce=True,
    )
    # 2. Every ~5 days: check retirement dates & milestone countdowns
    scheduler.add_job(
        monitor.check_all_retirement_dates,
        trigger="interval",
        days=settings.retirement_check_interval_days,
        id="retirement_scan_job",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info(
        "Scheduler started: price check every %d min, retirement check every %d days.",
        settings.price_check_interval_minutes,
        settings.retirement_check_interval_days,
    )

    # Trigger an initial background scan shortly after startup
    asyncio.create_task(monitor.scan_all_subscribed_sets())

    try:
        logger.info("Starting Telegram Bot long-polling...")
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="LEGO Cheapest Sets Parser & Telegram Bot")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single price scan cycle and exit (for GitHub Actions cron)",
    )
    parser.add_argument(
        "--check-retirement",
        action="store_true",
        help="Also check retirement dates during --once run",
    )
    args = parser.parse_args()

    if args.once or args.check_retirement:
        asyncio.run(run_once(check_retirement=args.check_retirement))
    else:
        asyncio.run(run_daemon())


if __name__ == "__main__":
    main()
