-- longterm_schema.sql — the weekly long-term book (EXECUTOR_DESIGN.md §2.1): advice for the
-- MAIN account, which the agent can only read. Nothing here is an order.
-- Run once in the Supabase SQL editor, after approval_schema.sql (it uses _is_trading_admin).
-- Re-running is safe.

-- One row per ticker per weekly run, written by build_longterm.py (rules, no model calls).
-- action: NEW | ADD | TRIM | EXIT | HOLD | AVOID (0% target, not held), against the main account's current weight when a
-- holdings snapshot exists, otherwise against last week's target.
CREATE TABLE IF NOT EXISTS longterm_advice (
    id               BIGSERIAL PRIMARY KEY,
    run_id           TEXT        NOT NULL,
    week_of          DATE        NOT NULL,
    ticker           TEXT        NOT NULL,
    action           TEXT        NOT NULL,
    target_pct       NUMERIC(6, 2) NOT NULL,
    prev_target_pct  NUMERIC(6, 2),
    basis            TEXT        NOT NULL,     -- HOLDINGS (vs current weight) | LAST_WEEK (vs last target)
    conviction       SMALLINT,                 -- 1–3
    signal           TEXT,
    signal_strength  TEXT,
    valuation_view   TEXT,
    risk_score       NUMERIC(4, 1),
    sector           TEXT,
    why              JSONB       NOT NULL DEFAULT '[]',   -- [{"source": "...", "text": "..."}] ≤ 3
    wrong_if         TEXT,
    changes          TEXT[],                  -- what moved since last week
    regime           TEXT,
    cash_pct         NUMERIC(6, 2),           -- the plan's cash reserve (same on every row)
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_longterm_advice_week ON longterm_advice (week_of DESC);
-- Actions are measured against the real weights, so admins only: private_access_schema.sql.
ALTER TABLE longterm_advice ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "read longterm_advice" ON longterm_advice;

-- The main account's weights, from a weekly read-only snapshot (save_holdings.py). Only
-- each holding's percentage is stored: no share counts, dollar values or account size.
-- Readable only by signed-in trading admins (the dashboard's "Now" column), never anon.
CREATE TABLE IF NOT EXISTS longterm_holdings (
    snapshot_date  DATE        NOT NULL,
    ticker         TEXT        NOT NULL,
    weight_pct     NUMERIC(6, 2) NOT NULL,     -- % of the account's equity (cash excluded from rows)
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (snapshot_date, ticker)
);
ALTER TABLE longterm_holdings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "admins read longterm_holdings" ON longterm_holdings;
CREATE POLICY "admins read longterm_holdings" ON longterm_holdings FOR SELECT TO authenticated
    USING (_is_trading_admin());

NOTIFY pgrst, 'reload schema';
