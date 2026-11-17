from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    telegram_bot_token: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    )
    telegram_chat_id: int | None = field(
        default_factory=lambda: (
            int(os.getenv("TELEGRAM_CHAT_ID", "").strip())
            if os.getenv("TELEGRAM_CHAT_ID", "").strip().lstrip("-").isdigit()
            else None
        )
    )
    default_subscriptions: str = field(
        default_factory=lambda: os.getenv("DEFAULT_SUBSCRIPTIONS", "").strip()
    )
    database_path: Path = field(
        default_factory=lambda: Path(os.getenv("DATABASE_PATH", "data/lego_tracker.db"))
    )
    price_check_interval_minutes: int = field(
        default_factory=lambda: int(os.getenv("PRICE_CHECK_INTERVAL_MINUTES", "5"))
    )
    retirement_check_interval_days: int = field(
        default_factory=lambda: int(os.getenv("RETIREMENT_CHECK_INTERVAL_DAYS", "5"))
    )

    # Anti-blocking / Proxy configuration
    proxy_urls: list[str] = field(
        default_factory=lambda: [
            p.strip()
            for p in os.getenv("PROXY_URLS", "").split(",")
            if p.strip()
        ]
    )
    cf_worker_proxy_url: str = field(
        default_factory=lambda: os.getenv("CF_WORKER_PROXY_URL", "").strip()
    )
    cf_worker_secret: str = field(
        default_factory=lambda: os.getenv("CF_WORKER_SECRET", "").strip()
    )
    min_domain_delay_sec: float = field(
        default_factory=lambda: float(os.getenv("MIN_DOMAIN_DELAY_SEC", "1.2"))
    )
    max_domain_delay_sec: float = field(
        default_factory=lambda: float(os.getenv("MAX_DOMAIN_DELAY_SEC", "3.0"))
    )

    # Optional official API keys
    allegro_client_id: str = field(
        default_factory=lambda: os.getenv("ALLEGRO_CLIENT_ID", "").strip()
    )
    allegro_client_secret: str = field(
        default_factory=lambda: os.getenv("ALLEGRO_CLIENT_SECRET", "").strip()
    )
    ebay_client_id: str = field(
        default_factory=lambda: os.getenv("EBAY_CLIENT_ID", "").strip()
    )
    ebay_client_secret: str = field(
        default_factory=lambda: os.getenv("EBAY_CLIENT_SECRET", "").strip()
    )
    brickset_api_key: str = field(
        default_factory=lambda: os.getenv("BRICKSET_API_KEY", "").strip()
    )
    bricklink_consumer_key: str = field(
        default_factory=lambda: os.getenv("BRICKLINK_CONSUMER_KEY", "").strip()
    )
    bricklink_consumer_secret: str = field(
        default_factory=lambda: os.getenv("BRICKLINK_CONSUMER_SECRET", "").strip()
    )
    bricklink_token_value: str = field(
        default_factory=lambda: os.getenv("BRICKLINK_TOKEN_VALUE", "").strip()
    )
    bricklink_token_secret: str = field(
        default_factory=lambda: os.getenv("BRICKLINK_TOKEN_SECRET", "").strip()
    )


settings = Settings()
