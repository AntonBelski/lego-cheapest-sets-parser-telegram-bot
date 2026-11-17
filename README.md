# 🧱 LEGO Poland & EU Cheapest Sets Parser + Telegram Bot

A Telegram bot and multi-site price & retirement monitor designed for LEGO collectors in Poland. Every **5 minutes**, it scans **13 Polish & European marketplaces and price aggregators** for **original LEGO sets** in **New (Sealed)** or **New (Open Box, Sealed Bags)** condition, converts all currencies to **PLN** using real-time **Narodowy Bank Polski (NBP)** exchange rates, calculates the **final price with cheapest delivery** (or explicitly labels `(+delivery)`), stores **all observed prices** in SQLite, and sends a Telegram notification featuring the **LEGO set photo + daily lowest price dynamics chart**.

---

## 🌐 Monitored Websites (13 Sources)

1. **`olx.pl`** — Queries OLX Poland's v1 JSON API, filtering for original LEGO sets in `New` or `Open Box, Sealed Bags` condition + Przesyłka OLX (InPost) delivery.
2. **`vinted.pl`** — Queries Vinted's v2 catalog API (`New with tags` & `New without tags` with sealed bags), converting EUR/CZK/PLN to PLN.
3. **`allegrolokalnie.pl`** — Parses secondary marketplace offers in `Nowe` condition + Smart!/delivery costs.
4. **`allegro.pl`** — Supports official Allegro OAuth2 REST API (`/offers/listing` sorted by `+withDeliveryPrice`) with automatic browser-impersonated web fallback.
5. **`promoklocki.pl`** — Fetches official **`lego.pl` catalog price (MSRP)**, set name, set image, historical price series, and live Polish store offers.
6. **`klocki.app`** — Polish LEGO price aggregator with store shipping costs.
7. **`ceneo.pl`** — Poland's largest price comparison engine with delivery cost extraction.
8. **`brickmerge.de`** — European LEGO price aggregator tracking **Amazon.de, Amazon.fr, Amazon.es, Amazon.it, Proshop, Alternate, JB Spielwaren**, converted from EUR to PLN.
9. **`ebay.pl`** — Tracks Buy-It-Now offers (`Condition 1000` New & `1500` New Other / Open Box Sealed Bags) shipping to Poland, sorted by lowest Price + Shipping.
10. **`bricklink.com`** — Parses BrickLink catalog & AJAX store listings for `New` (`Complete`/`Sealed`) sets, converted from EUR/USD/GBP/PLN to PLN with explicit `(+delivery)`.
11. **`smyk.com`**, **`empik.com`**, **`mediaexpert.pl`** — Direct parsers for Poland's top 3 LEGO retailers so flash promos are caught immediately.

---

## ✨ Key Features

- **Flexible `[Price From, Price To]` Range with Skip**:
  - Set `Price From` and `Price To` interactively in Telegram (`/add`) with **`⏭ Skip (0 PLN)`** and **`⏭ Skip (∞ Infinity)`** buttons, or in one line:
    - `/add 10316 - 1750` → `0 PLN – 1750 PLN`
    - `/add 10316 1200 1750` → `1200 PLN – 1750 PLN`
    - `/add 10316` → `0 PLN – ∞`
- **Authenticity & Condition Guard**:
  - Rejects replicas/knock-offs (`Lepin`, `Mould King`, `kompatybilne`, `zamiennik`), empty boxes (`puste pudełko`), instructions-only (`sama instrukcja`), LED kits, or sets missing minifigures (`bez figurek`).
  - Accepts **New (Sealed)** and **New (Open Box, Sealed Bags)** (`otwarte pudełko, woreczki zamknięte` / `open box, sealed bags`), while rejecting used/built sets.
- **Full Price History & Smart Deduplication**:
  - **Every** valid price across all sites is stored in the database (`price_observations` & `daily_lowest_prices`), even when outside your target price range.
  - Computes **Lowest Price in Last 30 Days** and **Lowest Price ALL TIME** across all stored records.
  - **Smart Notification Cache (`notified_offers`)**: Never sends duplicate alerts for the same listing unless its total price drops by at least `1 PLN`.
- **Composite Set Photo + Daily Price Dynamics Chart**:
  - Every alert (and `/chart <set_number>`) generates a high-DPI PNG combining the **official LEGO set image**, **KPI summary**, and a **Daily Lowest Price Dynamics chart** with your target range shaded and `lego.pl` MSRP reference line.
- **Retirement Date Tracker (Every ~5 Days)**:
  - Checks **Brickset** (official LEGO ExitDate), **BrickEconomy**, and **Promoklocki**.
  - Alerts you **1)** the very first time a set receives a retirement date, and **2)** strictly at the requested countdown milestones:
    `1 year`, `9 months`, `8 months`, `7 months`, `6 months`, `5 months`, `4 months`, `3 months`, `2 months`, `1 month`, and `2 weeks`.
- **Anti-Blocking & IP Protection**:
  - Uses `curl_cffi` TLS/JA3/HTTP2 browser fingerprint impersonation (`chrome124` / `safari17_0`) + per-domain randomized jitter (`1.2s–3.0s`) + optional rotating proxies (`PROXY_URLS`) + included free **Cloudflare Worker relay** ([cloudflare_worker_proxy.js](file:///Users/antonbelski/Documents/Antigravity/lego-cheapest-sets-parser-telegram-bot/cloudflare_worker_proxy.js)).

---

## 🚀 Quick Start

### 1. Local / VPS / Docker Setup (Recommended 24/7 Instant Bot)

```bash
cp .env.example .env
# Edit .env and set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
```

**Run with Docker Compose:**
```bash
docker compose up -d --build
```

**Or run directly with Python:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m bot.main
```

### 2. GitHub Actions Setup (Serverless Cron Mode)

The workflow in [.github/workflows/lego_tracker.yml](file:///Users/antonbelski/Documents/Antigravity/lego-cheapest-sets-parser-telegram-bot/.github/workflows/lego_tracker.yml) runs `python -m bot.main --once` every 5 minutes and `--check-retirement` every 5 days, persisting the SQLite database (`data/lego_tracker.db`) to an orphan branch `db-state` so your `main` branch commit history stays clean.

Add the following **Repository Secrets** in GitHub (`Settings -> Secrets and variables -> Actions`):
- `TELEGRAM_BOT_TOKEN` — Your Telegram Bot token from `@BotFather`
- `TELEGRAM_CHAT_ID` — Your numeric Telegram user/chat ID
- `DEFAULT_SUBSCRIPTIONS` — Optional initial sets, e.g. `10316:-:1750,75192:2200:2900`
- `CF_WORKER_PROXY_URL` — Optional URL of your deployed [cloudflare_worker_proxy.js](file:///Users/antonbelski/Documents/Antigravity/lego-cheapest-sets-parser-telegram-bot/cloudflare_worker_proxy.js) to avoid Azure datacenter IP blocks.
