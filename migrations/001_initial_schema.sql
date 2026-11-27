-- DA Price Index — Initial Schema
-- Compatible with PostgreSQL. See inline notes for SQLite adaptations.
--
-- SQLite mapping:
--   BIGSERIAL            → INTEGER PRIMARY KEY AUTOINCREMENT
--   JSONB                → TEXT (store JSON strings)
--   TIMESTAMPTZ          → TEXT (ISO-8601 strings)
--   VARCHAR(N)           → TEXT
--   NUMERIC(p,s)         → REAL
--   SMALLINT             → INTEGER
--   BOOLEAN              → INTEGER (0/1)

-- 1. Metadata & provenance for each ingested DPI PDF
CREATE TABLE IF NOT EXISTS price_reports (
    report_id           BIGSERIAL PRIMARY KEY,
    region_code         VARCHAR(32) NOT NULL DEFAULT 'NCR',
    region_name         VARCHAR(128) NOT NULL,
    report_date         DATE NOT NULL,
    source_url          TEXT,
    file_name           TEXT NOT NULL,
    file_sha256         CHAR(64) NOT NULL UNIQUE,
    covered_markets     JSONB,
    covered_markets_cnt SMALLINT,
    parsed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    parser_version      VARCHAR(32) NOT NULL,
    UNIQUE (region_code, report_date)
);

-- 2. Canonical commodity dimension table (deduplicated across days)
CREATE TABLE IF NOT EXISTS commodities (
    commodity_id        BIGSERIAL PRIMARY KEY,
    top_category        VARCHAR(128) NOT NULL,
    sub_category        VARCHAR(128),
    group_category      VARCHAR(128),
    commodity_name      VARCHAR(255) NOT NULL,
    specification       TEXT NOT NULL DEFAULT '',
    unit                VARCHAR(32) NOT NULL,
    canonical_key       VARCHAR(512) NOT NULL UNIQUE,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3. Daily prevailing retail prices (fact table)
CREATE TABLE IF NOT EXISTS daily_commodity_prices (
    price_id            BIGSERIAL PRIMARY KEY,
    report_id           BIGINT NOT NULL REFERENCES price_reports(report_id) ON DELETE CASCADE,
    commodity_id        BIGINT NOT NULL REFERENCES commodities(commodity_id),
    report_date         DATE NOT NULL,
    price_prevailing    NUMERIC(10, 2),
    is_available        BOOLEAN NOT NULL,
    raw_price_text      VARCHAR(64) NOT NULL,
    page_number         SMALLINT NOT NULL,
    row_order           SMALLINT NOT NULL,
    is_drift_flagged    BOOLEAN NOT NULL DEFAULT FALSE,
    drift_pct           NUMERIC(6, 4),
    UNIQUE (report_id, commodity_id)
);

CREATE INDEX IF NOT EXISTS idx_daily_prices_date_commodity
    ON daily_commodity_prices (report_date DESC, commodity_id);

-- 4. Commodity aliases (maps renamed DA canonical keys to an existing commodity)
CREATE TABLE IF NOT EXISTS commodity_aliases (
    alias_id            BIGSERIAL PRIMARY KEY,
    commodity_id        BIGINT NOT NULL REFERENCES commodities(commodity_id) ON DELETE CASCADE,
    canonical_key       VARCHAR(512) NOT NULL UNIQUE,
    alias_note          VARCHAR(255),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_commodity_aliases_key
    ON commodity_aliases (canonical_key);
