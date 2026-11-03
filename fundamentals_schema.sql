-- ============================================================
-- Orblex Industries — Fundamentals Schema
-- Run this in your Supabase SQL Editor before deploying
-- analyst_agent.py v2 (sentiment_score / valuation_view).
-- ============================================================


-- ── STOCK FUNDAMENTALS ────────────────────────────────────────────────────────

-- Daily valuation snapshot populated by run_fundamentals.py (yfinance .info).
-- ETFs and new tickers will have most numeric columns NULL — stored as-is.
CREATE TABLE IF NOT EXISTS stock_fundamentals (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              TEXT,
    ticker              TEXT        NOT NULL,
    snapshot_date       DATE        NOT NULL,
    trailing_pe         NUMERIC(12, 5),
    forward_pe          NUMERIC(12, 5),
    peg_ratio           NUMERIC(12, 5),
    price_to_sales      NUMERIC(12, 5),
    price_to_book       NUMERIC(12, 5),
    dividend_yield      NUMERIC(10, 6),
    profit_margin       NUMERIC(10, 6),
    revenue_growth      NUMERIC(10, 6),
    earnings_growth     NUMERIC(10, 6),
    beta                NUMERIC(8, 5),
    market_cap          BIGINT,
    fifty_two_wk_high   NUMERIC(12, 4),
    fifty_two_wk_low    NUMERIC(12, 4),
    target_mean_price   NUMERIC(12, 4),
    recommendation_key  TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (ticker, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_stock_fundamentals_ticker ON stock_fundamentals (ticker);
CREATE INDEX IF NOT EXISTS idx_stock_fundamentals_date   ON stock_fundamentals (snapshot_date DESC);


-- ── AGENT REPORTS — add new analyst output columns ───────────────────────────

ALTER TABLE agent_reports
    ADD COLUMN IF NOT EXISTS sentiment_score NUMERIC(5, 3),    -- -1.0 to 1.0
    ADD COLUMN IF NOT EXISTS valuation_view  TEXT,             -- UNDERVALUED | FAIR | OVERVALUED | N/A
    ADD COLUMN IF NOT EXISTS news_count      INTEGER DEFAULT 0; -- headlines fed to the agent
