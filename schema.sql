-- ============================================================
-- Orblex Industries Stock Pipeline — Supabase Schema
-- Run this once in your Supabase SQL Editor before first run
-- ============================================================

-- 1. Raw daily OHLCV prices per ticker
CREATE TABLE IF NOT EXISTS stock_prices (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT        NOT NULL,
    date            DATE        NOT NULL,
    open            NUMERIC(12, 4),
    high            NUMERIC(12, 4),
    low             NUMERIC(12, 4),
    close           NUMERIC(12, 4),
    adj_close       NUMERIC(12, 4),
    volume          BIGINT,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (ticker, date)   -- prevents duplicate rows on re-runs
);

-- 2. Company metadata (fetched once per ticker)
CREATE TABLE IF NOT EXISTS stock_metadata (
    ticker          TEXT        PRIMARY KEY,
    company_name    TEXT,
    sector          TEXT,
    industry        TEXT,
    market_cap      BIGINT,
    currency        TEXT,
    exchange        TEXT,
    website         TEXT,
    description     TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3. Pipeline run log — audit trail of every execution
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              BIGSERIAL PRIMARY KEY,
    run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tickers         TEXT[],
    rows_inserted   INTEGER,
    rows_skipped    INTEGER,
    status          TEXT,       -- 'success' | 'partial' | 'failed'
    error_message   TEXT
);

-- Indexes for fast querying on the dashboard
CREATE INDEX IF NOT EXISTS idx_stock_prices_ticker      ON stock_prices (ticker);
CREATE INDEX IF NOT EXISTS idx_stock_prices_date        ON stock_prices (date DESC);
CREATE INDEX IF NOT EXISTS idx_stock_prices_ticker_date ON stock_prices (ticker, date DESC);
