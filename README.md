# DAPriceScraper

[![Tests](https://github.com/Gbot-Prod/DAPriceScraper/actions/workflows/tests.yml/badge.svg)](https://github.com/Gbot-Prod/DAPriceScraper/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **Unofficial project.** This is not affiliated with, endorsed by, or maintained by the Philippine Department of Agriculture. Prices are parsed from the DA's publicly published Daily Price Index PDFs; always treat the [original reports](https://www.da.gov.ph/price-monitoring/) as the authoritative source.

A standalone Python library and CLI tool (`da-dpi`) that monitors the Philippine Department of Agriculture (DA) Price Monitoring portal, downloads newly published **Daily Price Index (DPI)** PDFs, deterministically parses commodity retail price tables into validated structured data, and exports them to JSON/CSV or upserts them into a relational database (PostgreSQL / SQLite) for downstream e-commerce SKU benchmarking.

---

## Features

- **Robust PDF Parsing:** Line-coordinate bounded table extraction via `pdfplumber` handling multi-page repeated headers, multi-line row wrappings, and adaptive column layouts.
- **3-Tier Category State Machine:** Preserves category hierarchy (`top_category` → `sub_category` → `group_category`) across page splits and line breaks.
- **Deterministic Canonical Keys:** Stable commodity deduplication across days—minor layout and whitespace changes resolve to the exact same commodity dimension record.
- **Strict Validation Guardrails:** Sanity checks on row counts, negative/outlier prices, and day-over-day price drift (>50%).
- **Resilient Web Scraper:** Exponential retry backoff on network failures and transient HTTP 5xx/429 errors using `tenacity` and `httpx`.
- **Content-Addressable Deduplication:** `SHA-256` checksums and a local JSON manifest (`data/manifest.json`) ensure zero duplicate downloads or ingestions.
- **Relational DB Upserts:** Atomic, idempotent single-transaction upserts supporting SQLite and PostgreSQL via SQLAlchemy 2.0.
- **MCP Server:** Lets AI assistants (Claude, Cursor, VS Code) query prices through six read-only tools at `/mcp`.
- **HTTP Reference API:** Database-backed FastAPI service (`da-dpi serve`) with JSON/CSV responses, ETag caching, per-IP rate limiting, and commodity aliasing for DA renames.
- **Failure Alerts:** Scheduled sync exits non-zero on any failure and posts to a Slack/Discord webhook after 2 consecutive failures.
- **Rich CLI:** Modern command-line interface powered by `typer` and `rich`.

---

## Installation

```bash
# Clone the repository
git clone https://github.com/Gbot-Prod/DAPriceScraper.git
cd DAPriceScraper

# Install with pip or uv
pip install -e .

# Or for PostgreSQL support
pip install -e ".[postgres]"
```

The installed command is `da-dpi`, and the Python package imports as `da_price_index`.

---

## CLI Usage (`da-dpi`)

### 1. Parse a Local PDF
Parse a single PDF file and view a formatted terminal table:
```bash
da-dpi parse tests/fixtures/Daily-Price-Index-November-22-2026.pdf
```

Export structured data to JSON or CSV:
```bash
# JSON export
da-dpi parse tests/fixtures/Daily-Price-Index-November-22-2026.pdf --format json --output data/ncr_2026-11-22.json

# CSV export
da-dpi parse tests/fixtures/Daily-Price-Index-November-22-2026.pdf --format csv --output data/ncr_2026-11-22.csv
```

### 2. Discover & Download Recent PDFs
Scrape the DA portal for newly published Daily Price Index PDFs:
```bash
da-dpi fetch --limit 5
```

### 3. End-to-End Database Sync
Fetch, download, parse, validate (with signed drift annotation against previous database prices), and upsert reports into SQLite or PostgreSQL:
```bash
# SQLite (default)
da-dpi sync

# Sync a specific local file
da-dpi sync --file tests/fixtures/November-24-2026-DPI-AFC.pdf

# PostgreSQL target
da-dpi sync --db-url postgresql://user:password@localhost:5432/da_benchmarks
```

### 4. Start HTTP Reference API
Launch the FastAPI reference service serving JSON and RFC 4180 CSV:
```bash
da-dpi serve --port 8000
```

Available REST Endpoints:
- `GET /health` — Service status, version, and latest available report date.
- `GET /commodities?category=Meat&search=pork` — Canonical commodity catalog with stable `commodity_id`s.
- `GET /prices/latest?region=NCR&commodity_id=1&available_only=false` — Latest known prevailing retail price per commodity with ETag/Cache-Control caching.
- `GET /prices/latest?format=csv` (or `Accept: text/csv`) — Flat RFC 4180 CSV export.
- `GET /prices/summary?days=30&region=NCR&commodity_id=1` — Min, max, median, and latest prevailing price per commodity over a trailing window (1–366 days, ending on the latest report date). Omit `commodity_id` to get every commodity in one call.
- `GET /prices?commodity_id=1&from=2026-11-22&to=2026-11-24` — Historical time-series prices.
- `GET /reports/{date}` — Report provenance, hash, and covered wet markets list.

The same server hosts a public website: a landing page with a live price table at `/`, a full API guide at `/api-docs`, and the interactive OpenAPI explorer at `/docs`. The site files live in [src/da_price_index/site/](src/da_price_index/site/).

Example calls:
```bash
curl "http://localhost:8000/prices/latest?region=NCR&available_only=true"
curl "http://localhost:8000/prices/latest?format=csv" -o latest.csv
curl "http://localhost:8000/prices/summary?days=7&commodity_id=1&commodity_id=2"
curl "http://localhost:8000/prices?commodity_id=1&from=2026-11-01&to=2026-11-30"
```

### MCP server for AI assistants

The API also serves an [MCP](https://modelcontextprotocol.io) endpoint at `/mcp` (Streamable HTTP, stateless, no auth), so assistants like Claude, Cursor and VS Code can query prices directly. It exposes six read-only tools: `search_commodities`, `list_categories`, `get_latest_prices`, `get_price_summary`, `get_price_history` and `get_report_info`.

```bash
# Claude Code
claude mcp add --transport http dapricescraper https://your-host/mcp
```

Setup for other clients is on the `/api-docs#mcp` page. The tools live in [src/da_price_index/mcp_server.py](src/da_price_index/mcp_server.py).

Requests are rate limited per client IP (default **60/minute, 1,000/hour, 5,000/day**; `/health` is exempt). Over the limit, the API returns `429` with a `Retry-After` header. CORS allows any origin for `GET` requests, without credentials.

### 5. Daily Scheduled Runner
```bash
da-dpi run-daily
```

Fetches the latest PDFs, then parses them and upserts them oldest first, so day-over-day drift is always computed against the previous day. The command **exits with code 1** if anything fails: the portal is unreachable, the portal page has no DPI links (layout change), a download fails, or a file fails to parse, validate or upsert.
Every run is recorded in the `job_runs` table. When `DA_DPI_ALERT_WEBHOOK_URL` is set (a Slack or Discord incoming webhook), the command posts an alert on the **2nd consecutive failure** and a recovery notice on the next success. If the database itself is unreachable, it alerts immediately.

---

## Deployment (Docker)

The single image serves both roles:

```bash
docker build -t da-dpi .
docker run -p 8000:8000 -e DA_DPI_DB_URL=postgresql://... da-dpi            # API (listens on $PORT, default 8000)
docker run -e DA_DPI_DB_URL=postgresql://... da-dpi da-dpi run-daily        # scheduled sync job
```

Plain `postgres://` / `postgresql://` URLs (as handed out by Railway, Heroku, etc.) are routed to the bundled psycopg 3 driver automatically. The API creates missing tables on startup.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DA_DPI_DB_URL` | `sqlite:///data/da_price_index.db` | Database URL |
| `DA_DPI_ALERT_WEBHOOK_URL` | *(unset: alerts are logged only)* | Slack/Discord webhook for job alerts |
| `DA_DPI_ALERT_AFTER_FAILURES` | `2` | Consecutive failures before alerting |
| `DA_DPI_RATE_LIMIT_ENABLED` | `true` | Toggle per-IP rate limiting |
| `DA_DPI_RATE_LIMIT_PER_MINUTE` / `_PER_HOUR` / `_PER_DAY` | `60` / `1000` / `5000` | Per-IP limits (`0` disables a window) |
| `DA_DPI_CLIENT_IP_HEADER` | `x-real-ip` | Header the proxy uses for the real client IP (Railway sets `X-Real-IP`) |

Rate-limit counters live in process memory, so run **one API replica**. Each additional replica would multiply the effective limit.

---

## Relational Database Schema

```mermaid
erDiagram
    price_reports ||--o{ daily_commodity_prices : "contains"
    commodities ||--o{ daily_commodity_prices : "prices for"
    commodities ||--o{ commodity_aliases : "aliased by"

    price_reports {
        bigint report_id PK
        varchar region_code
        varchar region_name
        date report_date
        text file_name
        char file_sha256 UK
        jsonb covered_markets
        smallint covered_markets_cnt
        timestamptz parsed_at
        varchar parser_version
    }

    commodities {
        bigint commodity_id PK
        varchar top_category
        varchar sub_category
        varchar group_category
        varchar commodity_name
        text specification
        varchar unit
        varchar canonical_key UK
        boolean is_active
        timestamptz created_at
    }

    commodity_aliases {
        bigint alias_id PK
        bigint commodity_id FK
        varchar canonical_key UK
        varchar alias_note
        timestamptz created_at
    }

    daily_commodity_prices {
        bigint price_id PK
        bigint report_id FK
        bigint commodity_id FK
        date report_date
        numeric price_prevailing
        boolean is_available
        varchar raw_price_text
        smallint page_number
        smallint row_order
        boolean is_drift_flagged
        numeric drift_pct
    }
```

---

## Automation (GitHub Actions Example)

Schedule twice daily (e.g. `10:00 AM` and `4:00 PM` PHT / `02:00` and `08:00` UTC):

```yaml
name: Daily DA Price Sync

on:
  schedule:
    - cron: '0 2,8 * * *'
  workflow_dispatch:

jobs:
  sync-prices:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7

      - name: Set up Python
        uses: actions/setup-python@v7
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install dependencies
        run: pip install -e ".[postgres]"

      - name: Run daily sync
        env:
          DA_DPI_DB_URL: ${{ secrets.DATABASE_URL }}
        run: da-dpi run-daily
```

---

## Running Tests

```bash
pip install -e ".[dev]"
python run_tests.py
```

The PDFs in `tests/fixtures/` are unmodified copies of public Daily Price Index reports published by the DA, included as parser test fixtures.

---

## Responsible Use

The scraper targets a public government website. Please keep it polite:

- The default schedule (twice a day) is plenty, since the DA publishes one report per day. Don't poll the portal more often than that.
- Downloads are deduplicated by checksum, so re-running `sync` won't re-download files you already have.
- If you need historical data, sync once into your own database and serve it from there via `da-dpi serve`, instead of re-scraping.

---

## Contributing

Issues and pull requests are welcome. The DA occasionally changes its PDF layout; if parsing breaks for a new report, opening an issue with the report's date (or attaching the PDF) is the most useful thing you can do.

---

## License

[MIT](LICENSE)
