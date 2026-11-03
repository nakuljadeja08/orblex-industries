-- ============================================================
-- Orblex Industries — Signal Drift Tracker Schema
-- Run this in your Supabase SQL Editor BEFORE running run_drift.py
-- ============================================================

-- One row per ticker per analyst run — append-only signal log
CREATE TABLE IF NOT EXISTS signal_history (
    id               BIGSERIAL PRIMARY KEY,
    run_id           TEXT        NOT NULL,
    ticker           TEXT        NOT NULL,
    report_date      DATE        NOT NULL,
    signal           TEXT,                    -- BUY / SELL / HOLD
    signal_strength  TEXT,                    -- STRONG / MODERATE / WEAK
    close_price      NUMERIC(12, 4),
    rsi_14           NUMERIC(6, 2),
    ma_5             NUMERIC(12, 4),
    ma_20            NUMERIC(12, 4),
    volatility_ann   NUMERIC(8, 4),
    volume_ratio     NUMERIC(8, 4),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(run_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_sh_ticker      ON signal_history(ticker);
CREATE INDEX IF NOT EXISTS idx_sh_report_date ON signal_history(report_date DESC);

-- One row per detected signal flip per run
CREATE TABLE IF NOT EXISTS signal_drift (
    id               BIGSERIAL PRIMARY KEY,
    run_id           TEXT        NOT NULL,
    ticker           TEXT        NOT NULL,
    flip_date        DATE        NOT NULL,
    signal_from      TEXT,
    signal_to        TEXT,
    strength_from    TEXT,
    strength_to      TEXT,
    price_at_flip    NUMERIC(12, 4),
    flip_severity    TEXT,                    -- MAJOR / MODERATE / MINOR
    flip_count_7d    INTEGER,
    flip_count_30d   INTEGER,
    is_chronic       BOOLEAN     DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(run_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_sd_ticker    ON signal_drift(ticker);
CREATE INDEX IF NOT EXISTS idx_sd_flip_date ON signal_drift(flip_date DESC);

-- View used by the DRIFT dashboard tab
-- Returns one row per ticker with aggregate flip counts and latest flip info
CREATE OR REPLACE VIEW signal_drift_summary AS
SELECT
    sd.ticker,
    COUNT(*)                                                             AS total_flips,
    SUM(CASE WHEN sd.flip_date >= CURRENT_DATE - INTERVAL '7 days'
             THEN 1 ELSE 0 END)                                         AS flips_7d,
    SUM(CASE WHEN sd.flip_date >= CURRENT_DATE - INTERVAL '30 days'
             THEN 1 ELSE 0 END)                                         AS flips_30d,
    MAX(sd.flip_date)                                                    AS last_flip_date,
    BOOL_OR(sd.is_chronic)                                               AS ever_chronic,
    (SELECT sd2.signal_from
     FROM   signal_drift sd2
     WHERE  sd2.ticker = sd.ticker
     ORDER  BY sd2.flip_date DESC LIMIT 1)                              AS latest_signal_from,
    (SELECT sd2.signal_to
     FROM   signal_drift sd2
     WHERE  sd2.ticker = sd.ticker
     ORDER  BY sd2.flip_date DESC LIMIT 1)                              AS latest_signal_to,
    (SELECT sd2.flip_severity
     FROM   signal_drift sd2
     WHERE  sd2.ticker = sd.ticker
     ORDER  BY sd2.flip_date DESC LIMIT 1)                              AS latest_severity
FROM signal_drift sd
GROUP BY sd.ticker;
