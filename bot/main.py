from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot

from bot.config import settings
from bot.db import Database
from bot.services.http_client import http_client
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
        print(http_client.format_api_calls_summary(), flush=True)
        if bot:
            await bot.session.close()

    if monitor.last_run_failed_sites_count > 0:
        logger.error(
            "Exiting with code 1 because %d website check(s) failed during the run.",
            monitor.last_run_failed_sites_count,
        )
        sys.exit(1)


async def sync_db_state_to_git() -> None:
    """
    Atomically commit and push `data/lego_tracker.db` and `data/scraper_runs.log`
    to the orphan `db-state` branch using an isolated temporary git index
    (`GIT_INDEX_FILE`), without ever touching the working directory or `main` branch.
    """
    db_file = Path("data/lego_tracker.db")
    if not db_file.exists():
        return

    def _push_sync() -> None:
        idx_path = "/tmp/lego_db_state_git_index"
        if os.path.exists(idx_path):
            try:
                os.remove(idx_path)
            except OSError:
                pass
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = idx_path
        env.setdefault("GIT_AUTHOR_NAME", "github-actions[bot]")
        env.setdefault("GIT_AUTHOR_EMAIL", "github-actions[bot]@users.noreply.github.com")
        env.setdefault("GIT_COMMITTER_NAME", "github-actions[bot]")
        env.setdefault("GIT_COMMITTER_EMAIL", "github-actions[bot]@users.noreply.github.com")

        files = ["data/lego_tracker.db"]
        if Path("data/scraper_runs.log").exists():
            files.append("data/scraper_runs.log")

        subprocess.run(["git", "add", "-f", *files], env=env, check=True, capture_output=True)
        tree_sha = subprocess.check_output(["git", "write-tree"], env=env, text=True).strip()
        commit_sha = subprocess.check_output(
            ["git", "commit-tree", tree_sha, "-m", "Update LEGO tracker SQLite state and run logs"],
            env=env,
            text=True,
        ).strip()
        subprocess.run(
            ["git", "push", "-f", "origin", f"{commit_sha}:refs/heads/db-state"],
            env=env,
            check=True,
            capture_output=True,
        )

    try:
        await asyncio.to_thread(_push_sync)
        logger.info("Synced data/lego_tracker.db & scraper_runs.log to origin/db-state.")
    except Exception as exc:
        logger.warning("Live git db-state sync skipped/failed: %s", exc)


async def run_daemon(
    duration_minutes: Optional[int] = None,
    check_retirement: bool = False,
    sync_git_db: bool = False,
) -> None:
    """
    Run the 24/7 Telegram bot + 5-minute price scheduler + 5-day retirement scheduler.
    If `duration_minutes` is set (e.g. on GitHub Actions continuous runner mode),
    runs long-polling + scheduled scans for `duration_minutes` and then exits cleanly
    so the next queued GitHub Actions workflow run takes over seamlessly.
    """
    if not settings.telegram_bot_token or settings.telegram_bot_token == "your_telegram_bot_token_here":
        logger.error("TELEGRAM_BOT_TOKEN is not set in .env! Please configure .env first.")
        sys.exit(1)

    db = Database(settings.database_path)
    await db.init()

    bot = Bot(token=settings.telegram_bot_token)
    monitor = LegoMonitorService(db=db, bot=bot)
    await seed_default_subscriptions(db, monitor)

    dp = build_dispatcher(db, monitor)
    scan_lock = asyncio.Lock()
    any_scan_failed = False

    async def _scheduled_price_scan() -> None:
        nonlocal any_scan_failed
        try:
            async with scan_lock:
                await monitor.scan_all_subscribed_sets()
                if monitor.last_run_failed_sites_count > 0:
                    any_scan_failed = True
                if sync_git_db:
                    await sync_db_state_to_git()
        except asyncio.CancelledError:
            logger.info("Scheduled price scan cancelled cleanly during shutdown.")

    async def _scheduled_retirement_scan() -> None:
        try:
            async with scan_lock:
                await monitor.check_all_retirement_dates()
                if sync_git_db:
                    await sync_db_state_to_git()
        except asyncio.CancelledError:
            logger.info("Scheduled retirement scan cancelled cleanly during shutdown.")

    scheduler = AsyncIOScheduler()
    # 1. Every 5 minutes: check prices across all 13 Polish & EU sites
    scheduler.add_job(
        _scheduled_price_scan,
        trigger="interval",
        minutes=settings.price_check_interval_minutes,
        id="price_scan_job",
        max_instances=1,
        coalesce=True,
    )
    # 2. Every ~5 days: check retirement dates & milestone countdowns
    scheduler.add_job(
        _scheduled_retirement_scan,
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
    async def _initial_tasks() -> None:
        await _scheduled_price_scan()
        if check_retirement:
            await _scheduled_retirement_scan()

    init_task = asyncio.create_task(_initial_tasks())

    stop_timer_task: Optional[asyncio.Task] = None
    if duration_minutes and duration_minutes > 0:
        async def _stop_after_duration() -> None:
            # Stop 15 seconds before the 5-minute interval boundary so we never collide with a new scan tick
            sleep_sec = max(30, duration_minutes * 60 - 15)
            await asyncio.sleep(sleep_sec)
            logger.info(
                "Reached --daemon-minutes=%d window; waiting for active scan (if any) and stopping polling cleanly...",
                duration_minutes,
            )
            scheduler.shutdown(wait=False)
            async with scan_lock:
                try:
                    await dp.stop_polling()
                except Exception:
                    pass

        stop_timer_task = asyncio.create_task(_stop_after_duration())

    try:
        logger.info(
            "Starting Telegram Bot long-polling%s...",
            f" (24/7 GitHub Actions window: {duration_minutes} min)" if duration_minutes else "",
        )
        await dp.start_polling(bot, handle_signals=duration_minutes is None)
    finally:
        if stop_timer_task and not stop_timer_task.done():
            stop_timer_task.cancel()
        if not init_task.done():
            try:
                await asyncio.wait_for(init_task, timeout=45.0)
            except Exception:
                pass
        if scheduler.running:
            scheduler.shutdown(wait=False)
        if sync_git_db:
            await sync_db_state_to_git()
        print(http_client.format_api_calls_summary(), flush=True)
        await bot.session.close()

    if any_scan_failed:
        logger.error(
            "Exiting with code 1 because at least one website check failed during this run."
        )
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="LEGO Cheapest Sets Parser & Telegram Bot")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single price scan cycle and exit",
    )
    parser.add_argument(
        "--daemon-minutes",
        type=int,
        default=None,
        help="Run continuous 24/7 Telegram long-polling + 5-min scheduled scans for N minutes (for GitHub Actions)",
    )
    parser.add_argument(
        "--sync-git-db",
        action="store_true",
        help="Live-sync data/lego_tracker.db and data/scraper_runs.log to origin/db-state after each scan",
    )
    parser.add_argument(
        "--check-retirement",
        action="store_true",
        help="Also check retirement dates during startup/run",
    )
    args = parser.parse_args()

    if args.once:
        asyncio.run(run_once(check_retirement=args.check_retirement))
    else:
        asyncio.run(
            run_daemon(
                duration_minutes=args.daemon_minutes,
                check_retirement=args.check_retirement,
                sync_git_db=args.sync_git_db,
            )
        )


if __name__ == "__main__":
    main()
