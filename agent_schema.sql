-- ============================================================
-- Orblex Industries Agent Layer — Supabase Schema (add-on to schema.sql)
-- Run this in your Supabase SQL Editor
-- ============================================================

-- Agent analysis reports — one row per ticker per run
CREATE TABLE IF NOT EXISTS agent_reports (
    id              BIGSERIAL PRIMARY KEY,
    run_id          TEXT        NOT NULL,   -- groups all tickers from one agent run
    ticker          TEXT        NOT NULL,
    report_date     DATE        NOT NULL,   -- the "as of" date of the analysis
    signal          TEXT,                   -- 'BUY' | 'SELL' | 'HOLD'
    signal_strength TEXT,                   -- 'STRONG' | 'MODERATE' | 'WEAK'
    summary         TEXT,                   -- daily price & trend summary
    anomalies       JSONB,                  -- detected anomalies as structured JSON
    reasoning       TEXT,                   -- agent's full reasoning
    model           TEXT,                   -- which Claude model was used
    prompt_tokens   INTEGER,
    output_tokens   INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (run_id, ticker)
);

-- Aggregated run-level summary (one row per agent run across all tickers)
CREATE TABLE IF NOT EXISTS agent_run_log (
    id              BIGSERIAL PRIMARY KEY,
    run_id          TEXT        NOT NULL UNIQUE,
    run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tickers         TEXT[],
    reports_written INTEGER,
    status          TEXT,       -- 'success' | 'partial' | 'failed'
    error_message   TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_reports_ticker      ON agent_reports (ticker);
CREATE INDEX IF NOT EXISTS idx_agent_reports_report_date ON agent_reports (report_date DESC);
CREATE INDEX IF NOT EXISTS idx_agent_reports_signal      ON agent_reports (signal);
