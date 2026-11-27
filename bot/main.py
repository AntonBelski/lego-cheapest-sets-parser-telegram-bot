from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot

from bot.config import settings
from bot.db import Database
from bot.services.http_client import http_client
from bot.services.monitor import LegoMonitorService, WorkflowSessionStats
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
        if monitor.last_run_parser_summary:
            print(monitor.last_run_parser_summary, flush=True)
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
    scan_interval_minutes: Optional[int] = None,
) -> None:
    """
    Run the 24/7 Telegram bot + scheduled price scanner + retirement scheduler.
    If `duration_minutes` is set (e.g. 172 min for a 3-hour GitHub Actions window),
    runs long-polling + periodic scans every `scan_interval_minutes` (e.g. 15 min),
    exits cleanly before the next scheduled cron dispatch, and prints a full
    session summary of passed/failed checks and per-site success rates.
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
    lego_com_lock = asyncio.Lock()
    git_sync_lock = asyncio.Lock()
    any_scan_failed = False

    async def _sync_db_locked() -> None:
        if sync_git_db:
            async with git_sync_lock:
                await sync_db_state_to_git()

    effective_interval = (
        scan_interval_minutes
        if (scan_interval_minutes and scan_interval_minutes > 0)
        else settings.price_check_interval_minutes
    )
    session_stats = WorkflowSessionStats(
        interval_minutes=effective_interval,
        window_minutes=duration_minutes,
    )
    loop = asyncio.get_running_loop()
    deadline_mono: Optional[float] = (
        loop.time() + duration_minutes * 60
        if (duration_minutes and duration_minutes > 0)
        else None
    )

    async def _scheduled_price_scan(is_initial: bool = False) -> None:
        nonlocal any_scan_failed
        if not is_initial and deadline_mono is not None:
            remaining_sec = deadline_mono - loop.time()
            if remaining_sec < 180:
                logger.info(
                    "Skipping scheduled price scan because only %.0fs remain before clean shutdown.",
                    max(0.0, remaining_sec),
                )
                return
        try:
            async with scan_lock:
                await monitor.scan_all_subscribed_sets()
                session_stats.record_check(monitor.last_run_reports)
                if monitor.last_run_failed_sites_count > 0:
                    any_scan_failed = True
                await _sync_db_locked()
        except asyncio.CancelledError:
            logger.info("Scheduled price scan cancelled cleanly during shutdown.")

    async def _scheduled_lego_com_midnight_scan() -> None:
        if deadline_mono is not None:
            remaining_sec = deadline_mono - loop.time()
            if remaining_sec < 30:
                logger.info(
                    "Skipping LEGO.com midnight scan because only %.0fs remain before clean shutdown.",
                    max(0.0, remaining_sec),
                )
                return
        try:
            async with lego_com_lock:
                _, any_changed = await monitor.scan_lego_com_only()
                if any_changed:
                    await _sync_db_locked()
        except asyncio.CancelledError:
            logger.info("LEGO.com midnight scan cancelled cleanly during shutdown.")

    async def _scheduled_retirement_scan() -> None:
        try:
            async with scan_lock:
                await monitor.check_all_retirement_dates()
                await _sync_db_locked()
        except asyncio.CancelledError:
            logger.info("Scheduled retirement scan cancelled cleanly during shutdown.")

    try:
        warsaw_tz = ZoneInfo("Europe/Warsaw")
    except Exception:
        from datetime import timezone
        warsaw_tz = timezone.utc  # type: ignore[assignment]

    def _add_lego_com_midnight_jobs(sched: AsyncIOScheduler) -> None:
        sched.add_job(
            _scheduled_lego_com_midnight_scan,
            trigger="cron",
            hour="23,0",
            minute="58,59",
            timezone=warsaw_tz,
            id="lego_com_midnight_pre",
            max_instances=1,
            coalesce=True,
        )
        sched.add_job(
            _scheduled_lego_com_midnight_scan,
            trigger="cron",
            hour="0,1",
            minute="0-15",
            timezone=warsaw_tz,
            id="lego_com_midnight_post",
            max_instances=1,
            coalesce=True,
        )

    scheduler = AsyncIOScheduler()
    if not duration_minutes:
        # Permanent local 24/7 mode: repeat price check & retirement check indefinitely
        scheduler.add_job(
            _scheduled_price_scan,
            trigger="interval",
            minutes=effective_interval,
            id="price_scan_job",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            _scheduled_retirement_scan,
            trigger="interval",
            days=settings.retirement_check_interval_days,
            id="retirement_scan_job",
            max_instances=1,
            coalesce=True,
        )
        _add_lego_com_midnight_jobs(scheduler)
        scheduler.start()
        logger.info(
            "Scheduler started: price check every %d min, LEGO.com midnight check 23:58-00:15 & 00:58-01:15 Europe/Warsaw, retirement check every %d days.",
            effective_interval,
            settings.retirement_check_interval_days,
        )
    elif duration_minutes > effective_interval:
        # Multi-check GitHub Actions window (e.g. 3h workflow scanning every 15 min)
        scheduler.add_job(
            _scheduled_price_scan,
            trigger="interval",
            minutes=effective_interval,
            id="price_scan_job",
            max_instances=1,
            coalesce=True,
        )
        _add_lego_com_midnight_jobs(scheduler)
        scheduler.start()
        logger.info(
            "GitHub Actions polling mode (%d min): scanning at startup and every %d min (+ LEGO.com every min 23:58-00:15 & 00:58-01:15 Europe/Warsaw).",
            duration_minutes,
            effective_interval,
        )
    else:
        if duration_minutes >= 2:
            _add_lego_com_midnight_jobs(scheduler)
            scheduler.start()
        logger.info(
            "GitHub Actions polling mode (%d min): scanning all sites once at startup.",
            duration_minutes,
        )

    # Trigger an initial background scan shortly after startup so dp.start_polling() completes bot.get_me() first
    async def _initial_tasks() -> None:
        await asyncio.sleep(2.0)
        await _scheduled_price_scan(is_initial=True)
        if check_retirement:
            await _scheduled_retirement_scan()

    init_task = asyncio.create_task(_initial_tasks())

    stop_timer_task: Optional[asyncio.Task] = None
    if duration_minutes and duration_minutes > 0:
        async def _stop_after_duration() -> None:
            sleep_sec = max(30, duration_minutes * 60 - 15)
            await asyncio.sleep(sleep_sec)
            logger.info(
                "Reached --daemon-minutes=%d window; stopping polling cleanly...",
                duration_minutes,
            )
            try:
                if scheduler.running:
                    scheduler.shutdown(wait=False)
            except Exception as exc:
                logger.debug("Scheduler shutdown note: %s", exc)
            try:
                await asyncio.wait_for(asyncio.shield(scan_lock.acquire()), timeout=20.0)
                if scan_lock.locked():
                    scan_lock.release()
            except Exception:
                pass
            try:
                await dp.stop_polling()
            except Exception as exc:
                logger.debug("dp.stop_polling note: %s", exc)

        stop_timer_task = asyncio.create_task(_stop_after_duration())

    try:
        logger.info(
            "Starting Telegram Bot long-polling%s...",
            f" (GitHub Actions window: {duration_minutes} min, scan every {effective_interval} min)"
            if duration_minutes
            else "",
        )
        polling_coro = dp.start_polling(bot, handle_signals=duration_minutes is None)
        if duration_minutes and duration_minutes > 0:
            try:
                await asyncio.wait_for(polling_coro, timeout=duration_minutes * 60)
            except asyncio.TimeoutError:
                logger.info("Polling window (%d min) reached via hard timeout; exiting cleanly.", duration_minutes)
        else:
            await polling_coro
    finally:
        if stop_timer_task and not stop_timer_task.done():
            stop_timer_task.cancel()
        if not init_task.done():
            try:
                await asyncio.wait_for(init_task, timeout=45.0)
            except Exception:
                pass
        try:
            if scheduler.running:
                scheduler.shutdown(wait=False)
        except Exception:
            pass

        session_summary_text = (
            session_stats.format_text_summary()
            if session_stats.total_checks > 0
            else ""
        )
        if session_summary_text:
            try:
                log_file = Path(settings.database_path).parent / "scraper_runs.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                with log_file.open("a", encoding="utf-8") as f:
                    f.write(session_summary_text + "\n\n")
            except Exception as exc:
                logger.debug("Failed appending session summary to scraper_runs.log: %s", exc)

            gh_summary_path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
            if gh_summary_path:
                try:
                    with open(gh_summary_path, "a", encoding="utf-8") as f:
                        f.write(session_stats.format_markdown_summary())
                except Exception as exc:
                    logger.debug("Failed writing GITHUB_STEP_SUMMARY: %s", exc)

        await _sync_db_locked()
        print(http_client.format_api_calls_summary(), flush=True)
        if monitor.last_run_parser_summary:
            print(monitor.last_run_parser_summary, flush=True)
        if session_summary_text:
            print(session_summary_text, flush=True)
        await bot.session.close()

    if any_scan_failed:
        logger.error(
            "Exiting with code 1 because %d/%d scheduled check(s) had at least one failed website.",
            session_stats.failed_checks,
            session_stats.total_checks,
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
        help="Run continuous Telegram long-polling + scheduled scans for N minutes (for GitHub Actions)",
    )
    parser.add_argument(
        "--scan-interval-minutes",
        type=int,
        default=None,
        help="Override interval in minutes between scheduled price scans in daemon mode (default: PRICE_CHECK_INTERVAL_MINUTES)",
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
                scan_interval_minutes=args.scan_interval_minutes,
            )
        )


if __name__ == "__main__":
    main()
