from __future__ import annotations

import json
import aiosqlite
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from bot.models import Offer, PriceStats, ScraperSiteReport, SetMetadata, Subscription


class Database:
    """Async SQLite database manager for subscriptions, price history, deduplication, retirement alerts, and run logs."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA synchronous=NORMAL;")

            # 1. Subscriptions table
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    set_number TEXT NOT NULL,
                    price_from REAL NOT NULL DEFAULT 0.0,
                    price_to REAL NULL,
                    set_name TEXT NULL,
                    msrp_pln REAL NULL,
                    image_url TEXT NULL,
                    retirement_date TEXT NULL,
                    retirement_source TEXT NULL,
                    initial_scan_done INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(chat_id, set_number)
                );
                """
            )
            # Ensure initial_scan_done exists on older DBs
            try:
                await db.execute(
                    "ALTER TABLE subscriptions ADD COLUMN initial_scan_done INTEGER NOT NULL DEFAULT 0;"
                )
            except Exception:
                pass

            # 2. All price observations (stores ALL prices even outside user price range)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS price_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    set_number TEXT NOT NULL,
                    source TEXT NOT NULL,
                    store_name TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    item_price_original REAL NOT NULL,
                    currency_original TEXT NOT NULL,
                    item_price_pln REAL NOT NULL,
                    delivery_price_pln REAL NULL,
                    total_price_pln REAL NOT NULL,
                    previous_price_pln REAL NULL,
                    delivery_known INTEGER NOT NULL DEFAULT 0,
                    delivery_time TEXT NULL,
                    condition TEXT NOT NULL,
                    in_user_range INTEGER NOT NULL DEFAULT 0,
                    observed_date TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                """
            )
            try:
                await db.execute(
                    "ALTER TABLE price_observations ADD COLUMN previous_price_pln REAL NULL;"
                )
            except Exception:
                pass

            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_price_obs_set_date
                ON price_observations(set_number, observed_at, total_price_pln);
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_price_obs_listing
                ON price_observations(set_number, source, external_id, id);
                """
            )

            # 3. Daily lowest price dynamics table (1 row per set_number per calendar day)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_lowest_prices (
                    set_number TEXT NOT NULL,
                    day_date TEXT NOT NULL,
                    lowest_total_pln REAL NOT NULL,
                    source TEXT NOT NULL,
                    store_name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (set_number, day_date)
                );
                """
            )

            # 4. Smart notification deduplication cache
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS notified_offers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    set_number TEXT NOT NULL,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    offer_fingerprint TEXT NOT NULL,
                    notified_price_pln REAL NOT NULL,
                    url TEXT NOT NULL,
                    notified_at TEXT NOT NULL,
                    UNIQUE(chat_id, set_number, source, external_id)
                );
                """
            )

            # 5. Retirement milestone notifications log
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS retirement_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    set_number TEXT NOT NULL,
                    milestone_key TEXT NOT NULL,
                    retirement_date TEXT NOT NULL,
                    notified_at TEXT NOT NULL,
                    UNIQUE(chat_id, set_number, milestone_key, retirement_date)
                );
                """
            )

            # 6. Scraper health & diagnostics
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS scraper_health (
                    source TEXT PRIMARY KEY,
                    last_run_at TEXT NOT NULL,
                    last_status TEXT NOT NULL,
                    offers_count INTEGER NOT NULL DEFAULT 0,
                    urls_scraped TEXT NULL,
                    error_message TEXT NULL
                );
                """
            )
            try:
                await db.execute("ALTER TABLE scraper_health ADD COLUMN urls_scraped TEXT NULL;")
            except Exception:
                pass

            # 7. Persistent 5-minute run logs (stores every run + per-set per-website URLs & status)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    overall_status TEXT NOT NULL,
                    sets_scanned INTEGER NOT NULL DEFAULT 0,
                    total_offers_found INTEGER NOT NULL DEFAULT 0,
                    failed_sites_count INTEGER NOT NULL DEFAULT 0,
                    summary_log TEXT NOT NULL
                );
                """
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_run_site_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    set_number TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    urls_scraped_json TEXT NOT NULL,
                    offers_found INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT NULL,
                    logged_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES scan_runs(id)
                );
                """
            )

            await db.execute(
                """
                DELETE FROM price_observations
                WHERE LOWER(title) LIKE '%jak lego%'
                   OR LOWER(title) LIKE '%w stylu lego%';
                """
            )
            await db.execute(
                """
                DELETE FROM daily_lowest_prices
                WHERE set_number IN (SELECT set_number FROM subscriptions WHERE msrp_pln > 100)
                  AND lowest_total_pln < (
                      SELECT MAX(msrp_pln) * 0.18 FROM subscriptions s WHERE s.set_number = daily_lowest_prices.set_number
                  );
                """
            )

            await db.commit()

    # -------------------------------------------------------------------------
    # Subscriptions CRUD
    # -------------------------------------------------------------------------
    async def upsert_subscription(
        self,
        chat_id: int,
        set_number: str,
        price_from: Optional[float] = 0.0,
        price_to: Optional[float] = None,
        set_name: Optional[str] = None,
        msrp_pln: Optional[float] = None,
        image_url: Optional[str] = None,
    ) -> Subscription:
        set_number = str(set_number).strip()
        p_from = float(price_from) if price_from is not None else 0.0
        p_to = float(price_to) if price_to is not None else None

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                """
                INSERT INTO subscriptions (
                    chat_id, set_number, price_from, price_to, set_name, msrp_pln, image_url
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, set_number) DO UPDATE SET
                    price_from = excluded.price_from,
                    price_to = excluded.price_to,
                    set_name = COALESCE(excluded.set_name, subscriptions.set_name),
                    msrp_pln = COALESCE(excluded.msrp_pln, subscriptions.msrp_pln),
                    image_url = COALESCE(excluded.image_url, subscriptions.image_url);
                """,
                (chat_id, set_number, p_from, p_to, set_name, msrp_pln, image_url),
            )
            await db.commit()
            return await self.get_subscription(chat_id, set_number)  # type: ignore

    async def mark_initial_scan_done(self, set_number: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE subscriptions SET initial_scan_done = 1 WHERE set_number = ?;",
                (set_number,),
            )
            await db.commit()

    async def needs_initial_deep_scan(self, set_number: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT MIN(initial_scan_done) FROM subscriptions WHERE set_number = ?",
                (set_number,),
            ) as cursor:
                row = await cursor.fetchone()
                if not row or row[0] is None:
                    return True
                return int(row[0]) == 0

    async def update_set_metadata(self, meta: SetMetadata) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            ret_str = meta.retirement_date.isoformat() if meta.retirement_date else None
            await db.execute(
                """
                UPDATE subscriptions
                SET
                    set_name = COALESCE(?, set_name),
                    msrp_pln = COALESCE(?, msrp_pln),
                    image_url = COALESCE(?, image_url),
                    retirement_date = COALESCE(?, retirement_date),
                    retirement_source = COALESCE(?, retirement_source)
                WHERE set_number = ?;
                """,
                (
                    meta.name or None,
                    meta.msrp_pln,
                    meta.image_url,
                    ret_str,
                    meta.retirement_source,
                    meta.set_number,
                ),
            )
            if meta.msrp_pln and meta.msrp_pln > 100:
                floor_pln = meta.msrp_pln * 0.18
                await db.execute(
                    "DELETE FROM price_observations WHERE set_number = ? AND item_price_pln < ?",
                    (meta.set_number, floor_pln),
                )
                await db.execute(
                    "DELETE FROM daily_lowest_prices WHERE set_number = ? AND lowest_total_pln < ?",
                    (meta.set_number, floor_pln),
                )
            await db.commit()

    async def get_subscription(self, chat_id: int, set_number: str) -> Optional[Subscription]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM subscriptions WHERE chat_id = ? AND set_number = ?",
                (chat_id, str(set_number).strip()),
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return self._row_to_sub(row)

    async def list_subscriptions(self, chat_id: Optional[int] = None) -> list[Subscription]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if chat_id is not None:
                query = "SELECT * FROM subscriptions WHERE chat_id = ? ORDER BY set_number ASC"
                params = (chat_id,)
            else:
                query = "SELECT * FROM subscriptions ORDER BY set_number ASC"
                params = ()
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_sub(r) for r in rows]

    async def remove_subscription(self, chat_id: int, set_number: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM subscriptions WHERE chat_id = ? AND set_number = ?",
                (chat_id, str(set_number).strip()),
            )
            await db.commit()
            return cursor.rowcount > 0

    @staticmethod
    def _row_to_sub(row: aiosqlite.Row) -> Subscription:
        keys = row.keys()
        return Subscription(
            id=row["id"],
            chat_id=row["chat_id"],
            set_number=row["set_number"],
            price_from=row["price_from"] or 0.0,
            price_to=row["price_to"],
            set_name=row["set_name"],
            msrp_pln=row["msrp_pln"],
            image_url=row["image_url"],
            retirement_date=row["retirement_date"],
            retirement_source=row["retirement_source"],
            initial_scan_done=bool(row["initial_scan_done"]) if "initial_scan_done" in keys else False,
            created_at=row["created_at"],
        )

    # -------------------------------------------------------------------------
    # Price Observations, Price-Change Detection & Historical Stats
    # -------------------------------------------------------------------------
    async def record_offers(
        self,
        set_number: str,
        offers: list[Offer],
        subscriptions_for_set: list[Subscription],
    ) -> int:
        """
        Store ALL valid offers found across all sites (even outside user's price range),
        detect if any listing changed price compared to its previous observation
        (setting `offer.previous_price_pln`), and update the daily lowest price table.
        """
        if not offers:
            return 0

        inserted = 0
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.strftime("%Y-%m-%d %H:%M:%S")
        today_str = now_utc.strftime("%Y-%m-%d")

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            for offer in offers:
                in_range = any(
                    sub.matches_price(offer.total_price_pln)
                    for sub in subscriptions_for_set
                )

                # Look up the most recent observation of this exact listing across ALL days
                async with db.execute(
                    """
                    SELECT total_price_pln, observed_date FROM price_observations
                    WHERE set_number = ? AND source = ? AND external_id = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (set_number, offer.source, offer.external_id),
                ) as cursor:
                    prev = await cursor.fetchone()

                prev_price: Optional[float] = None
                if prev is not None:
                    last_seen_price = float(prev["total_price_pln"])
                    if abs(last_seen_price - offer.total_price_pln) >= 0.50:
                        prev_price = last_seen_price
                        offer.previous_price_pln = last_seen_price

                # Insert if never seen before, or seen on an earlier date, or its price changed
                should_insert = (
                    prev is None
                    or prev_price is not None
                    or str(prev["observed_date"]) != today_str
                )
                if should_insert:
                    await db.execute(
                        """
                        INSERT INTO price_observations (
                            set_number, source, store_name, external_id, title, url,
                            item_price_original, currency_original, item_price_pln,
                            delivery_price_pln, total_price_pln, previous_price_pln,
                            delivery_known, delivery_time, condition, in_user_range,
                            observed_date, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            set_number,
                            offer.source,
                            offer.store_name,
                            offer.external_id,
                            offer.title,
                            offer.url,
                            offer.item_price_original,
                            offer.currency_original,
                            offer.item_price_pln,
                            offer.delivery_price_pln,
                            offer.total_price_pln,
                            offer.previous_price_pln,
                            1 if offer.delivery_known else 0,
                            offer.delivery_time,
                            offer.condition,
                            1 if in_range else 0,
                            today_str,
                            now_iso,
                        ),
                    )
                    inserted += 1

            # Update daily lowest price for today
            cheapest = min(offers, key=lambda o: o.total_price_pln)
            await db.execute(
                """
                INSERT INTO daily_lowest_prices (
                    set_number, day_date, lowest_total_pln, source, store_name, url, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(set_number, day_date) DO UPDATE SET
                    lowest_total_pln = CASE
                        WHEN excluded.lowest_total_pln < daily_lowest_prices.lowest_total_pln
                        THEN excluded.lowest_total_pln
                        ELSE daily_lowest_prices.lowest_total_pln
                    END,
                    source = CASE
                        WHEN excluded.lowest_total_pln < daily_lowest_prices.lowest_total_pln
                        THEN excluded.source
                        ELSE daily_lowest_prices.source
                    END,
                    store_name = CASE
                        WHEN excluded.lowest_total_pln < daily_lowest_prices.lowest_total_pln
                        THEN excluded.store_name
                        ELSE daily_lowest_prices.store_name
                    END,
                    url = CASE
                        WHEN excluded.lowest_total_pln < daily_lowest_prices.lowest_total_pln
                        THEN excluded.url
                        ELSE daily_lowest_prices.url
                    END,
                    updated_at = excluded.updated_at;
                """,
                (
                    set_number,
                    today_str,
                    cheapest.total_price_pln,
                    cheapest.source,
                    cheapest.store_name,
                    cheapest.url,
                    now_iso,
                ),
            )
            await db.commit()
        return inserted

    async def seed_historical_daily_prices(
        self,
        set_number: str,
        daily_points: list[tuple[str, float, str]],
    ) -> None:
        if not daily_points:
            return
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with aiosqlite.connect(self.db_path) as db:
            for day_date, price_pln, source in daily_points:
                if price_pln <= 0:
                    continue
                await db.execute(
                    """
                    INSERT INTO daily_lowest_prices (
                        set_number, day_date, lowest_total_pln, source, store_name, url, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(set_number, day_date) DO UPDATE SET
                        lowest_total_pln = MIN(daily_lowest_prices.lowest_total_pln, excluded.lowest_total_pln);
                    """,
                    (
                        set_number,
                        day_date,
                        round(price_pln, 2),
                        source,
                        source,
                        f"https://promoklocki.pl/{set_number}",
                        now_iso,
                    ),
                )
            await db.commit()

    async def get_price_stats(self, set_number: str) -> PriceStats:
        stats = PriceStats(set_number=set_number)
        cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            async with db.execute(
                """
                SELECT lowest_total_pln AS price, source, day_date AS dt
                FROM daily_lowest_prices
                WHERE set_number = ? AND day_date >= ?
                ORDER BY lowest_total_pln ASC LIMIT 1
                """,
                (set_number, cutoff_30d),
            ) as cursor:
                row_30d = await cursor.fetchone()
                if row_30d:
                    stats.lowest_30d_pln = float(row_30d["price"])
                    stats.lowest_30d_source = row_30d["source"]
                    stats.lowest_30d_date = row_30d["dt"]

            async with db.execute(
                """
                SELECT lowest_total_pln AS price, source, day_date AS dt
                FROM daily_lowest_prices
                WHERE set_number = ?
                ORDER BY lowest_total_pln ASC LIMIT 1
                """,
                (set_number,),
            ) as cursor:
                row_all = await cursor.fetchone()
                if row_all:
                    stats.lowest_all_time_pln = float(row_all["price"])
                    stats.lowest_all_time_source = row_all["source"]
                    stats.lowest_all_time_date = row_all["dt"]

            async with db.execute(
                """
                SELECT day_date, lowest_total_pln
                FROM daily_lowest_prices
                WHERE set_number = ?
                ORDER BY day_date ASC
                LIMIT 90
                """,
                (set_number,),
            ) as cursor:
                rows = await cursor.fetchall()
                stats.daily_lowest_series = [
                    (r["day_date"], float(r["lowest_total_pln"])) for r in rows
                ]

        return stats

    async def get_latest_offers(self, set_number: str, limit: int = 10) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT source, store_name, title, url, item_price_pln, delivery_price_pln,
                       total_price_pln, previous_price_pln, delivery_known, delivery_time,
                       condition, MAX(observed_at) as seen_at
                FROM price_observations
                WHERE set_number = ?
                  AND observed_date >= date('now', '-2 days')
                GROUP BY source, external_id
                ORDER BY total_price_pln ASC
                LIMIT ?
                """,
                (set_number, limit),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    # -------------------------------------------------------------------------
    # Smart Notification Deduplication & Price-Drop Cache
    # -------------------------------------------------------------------------
    async def should_notify_offer(self, chat_id: int, offer: Offer) -> bool:
        """
        Returns True if:
        1) This specific offer `(source, external_id)` has NEVER been notified to `chat_id`
           (including when it was previously observed at e.g. 2000 PLN outside the user's
           range, and now dropped to 1500 PLN inside the user's range!), OR
        2) It WAS previously notified to `chat_id` (e.g. at 1750 PLN), and the seller
           dropped the price by at least 1.00 PLN (e.g. to 1500 PLN).
           In that case, `offer.previous_price_pln` is also populated with the last notified
           price if not already set.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT notified_price_pln, offer_fingerprint
                FROM notified_offers
                WHERE chat_id = ? AND set_number = ? AND source = ? AND external_id = ?
                """,
                (chat_id, offer.set_number, offer.source, offer.external_id),
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return True
                prev_notified = float(row["notified_price_pln"])
                if (prev_notified - offer.total_price_pln) >= 1.0:
                    if offer.previous_price_pln is None:
                        offer.previous_price_pln = prev_notified
                    return True
                return False

    async def mark_offer_notified(self, chat_id: int, offer: Offer) -> None:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO notified_offers (
                    chat_id, set_number, source, external_id,
                    offer_fingerprint, notified_price_pln, url, notified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, set_number, source, external_id) DO UPDATE SET
                    offer_fingerprint = excluded.offer_fingerprint,
                    notified_price_pln = excluded.notified_price_pln,
                    url = excluded.url,
                    notified_at = excluded.notified_at;
                """,
                (
                    chat_id,
                    offer.set_number,
                    offer.source,
                    offer.external_id,
                    offer.offer_fingerprint,
                    offer.total_price_pln,
                    offer.url,
                    now_iso,
                ),
            )
            await db.commit()

    # -------------------------------------------------------------------------
    # Retirement Date Milestone Cache
    # -------------------------------------------------------------------------
    async def has_sent_retirement_milestone(
        self,
        chat_id: int,
        set_number: str,
        milestone_key: str,
        retirement_date: str,
    ) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT 1 FROM retirement_notifications
                WHERE chat_id = ? AND set_number = ? AND milestone_key = ? AND retirement_date = ?
                """,
                (chat_id, set_number, milestone_key, retirement_date),
            ) as cursor:
                return (await cursor.fetchone()) is not None

    async def mark_retirement_milestone_sent(
        self,
        chat_id: int,
        set_number: str,
        milestone_key: str,
        retirement_date: str,
    ) -> None:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO retirement_notifications (
                    chat_id, set_number, milestone_key, retirement_date, notified_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, set_number, milestone_key, retirement_date, now_iso),
            )
            await db.commit()

    # -------------------------------------------------------------------------
    # Scraper Health & Persistent Run Logs
    # -------------------------------------------------------------------------
    async def record_scraper_health(
        self,
        source: str,
        status: str,
        offers_count: int = 0,
        urls_scraped: Optional[list[str]] = None,
        error_message: Optional[str] = None,
    ) -> None:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        urls_json = json.dumps(urls_scraped or [], ensure_ascii=False)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO scraper_health (source, last_run_at, last_status, offers_count, urls_scraped, error_message)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    last_run_at = excluded.last_run_at,
                    last_status = excluded.last_status,
                    offers_count = excluded.offers_count,
                    urls_scraped = excluded.urls_scraped,
                    error_message = excluded.error_message;
                """,
                (source, now_iso, status, offers_count, urls_json, error_message),
            )
            await db.commit()

    async def get_scraper_health(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM scraper_health ORDER BY source ASC"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def record_scan_run_logs(
        self,
        started_at: str,
        finished_at: str,
        overall_status: str,
        sets_scanned: int,
        total_offers_found: int,
        failed_sites_count: int,
        summary_log: str,
        site_reports: list[ScraperSiteReport],
    ) -> int:
        """
        Persist the complete 5-minute run summary and per-set, per-website URLs & status
        into SQLite (`scan_runs` and `scan_run_site_logs`).
        """
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO scan_runs (
                    started_at, finished_at, overall_status, sets_scanned,
                    total_offers_found, failed_sites_count, summary_log
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    started_at,
                    finished_at,
                    overall_status,
                    sets_scanned,
                    total_offers_found,
                    failed_sites_count,
                    summary_log,
                ),
            )
            run_id = int(cursor.lastrowid or 0)

            for rep in site_reports:
                await db.execute(
                    """
                    INSERT INTO scan_run_site_logs (
                        run_id, set_number, source, status, urls_scraped_json,
                        offers_found, error_message, logged_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        rep.set_number,
                        rep.source,
                        "SUCCESS" if rep.success else "FAILED",
                        json.dumps(rep.urls_scraped, ensure_ascii=False),
                        len(rep.offers),
                        rep.error_message,
                        now_iso,
                    ),
                )

            # Keep last 500 scan runs so DB stays compact
            await db.execute(
                """
                DELETE FROM scan_run_site_logs
                WHERE run_id NOT IN (SELECT id FROM scan_runs ORDER BY id DESC LIMIT 500);
                """
            )
            await db.execute(
                """
                DELETE FROM scan_runs
                WHERE id NOT IN (SELECT id FROM scan_runs ORDER BY id DESC LIMIT 500);
                """
            )
            await db.commit()
            return run_id

    async def get_recent_scan_runs(self, limit: int = 3) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ) as cursor:
                runs = [dict(r) for r in await cursor.fetchall()]

            for r in runs:
                async with db.execute(
                    "SELECT * FROM scan_run_site_logs WHERE run_id = ? ORDER BY set_number ASC, source ASC",
                    (r["id"],),
                ) as c2:
                    r["site_logs"] = [dict(sl) for sl in await c2.fetchall()]
            return runs
