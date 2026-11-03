-- universe_schema.sql — account balance history and the daily swing universe.
-- Run once in the Supabase SQL editor. Re-running is safe.

-- The agentic account as the morning trade-plan task read it (Robinhood MCP,
-- read-only). The 7:45 pipeline sizes tomorrow's universe from the latest row.
CREATE TABLE IF NOT EXISTS account_snapshots (
    id             BIGSERIAL PRIMARY KEY,
    as_of          TIMESTAMPTZ NOT NULL,
    snapshot_date  DATE        NOT NULL,     -- New York date of as_of
    source         TEXT,                     -- robinhood
    equity         NUMERIC(14, 2) NOT NULL,
    buying_power   NUMERIC(14, 2),
    cash           NUMERIC(14, 2),
    positions      JSONB,                    -- [{ticker, qty, avg_cost, last_price}]
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_account_snapshots_asof ON account_snapshots (as_of DESC);

-- Every ticker the universe stage considered on a run, and why it was or was
-- not analysed (run_universe.py / universe_selector.py).
CREATE TABLE IF NOT EXISTS universe_runs (
    run_id        TEXT        NOT NULL,
    run_date      DATE        NOT NULL,
    ticker        TEXT        NOT NULL,
    source        TEXT        NOT NULL,      -- HELD | CORE | SLEEVE
    included      BOOLEAN     NOT NULL,
    reason        TEXT,
    rank          INTEGER,                   -- sleeve rank by relative strength
    price         NUMERIC(14, 4),
    atr_pct       NUMERIC(8, 2),
    dollar_vol_m  NUMERIC(12, 1),
    max_qty       INTEGER,                   -- shares sizeable at a normal risk level
    rs20          NUMERIC(8, 2),             -- 20-day relative strength vs SPY, points
    sector        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_universe_runs_date ON universe_runs (run_date DESC);

-- account_snapshots holds the account balance: no read policy at all, so only
-- the pipeline's service key can read it — never the dashboard's public key.
ALTER TABLE account_snapshots ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "read account_snapshots" ON account_snapshots;
-- universe_runs has no balance column, but HELD rows and max_qty reveal the account, so
-- admins read it through the signed-in dashboard: private_access_schema.sql.
ALTER TABLE universe_runs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "read universe_runs" ON universe_runs;

NOTIFY pgrst, 'reload schema';
