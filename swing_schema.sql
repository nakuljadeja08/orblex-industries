-- swing_schema.sql — swing-mode trade plan columns on agent_reports
-- (TRADING_MODE=swing, see swing_policy.py). Run once in the Supabase SQL editor.
-- All columns are NULL on position-mode rows and on rows written before this
-- migration, so older reports read as "no plan" rather than a made-up one.

ALTER TABLE agent_reports
    ADD COLUMN IF NOT EXISTS trading_mode  TEXT,            -- 'position' (NULL) | 'swing'
    ADD COLUMN IF NOT EXISTS setup_type    TEXT,            -- BREAKOUT | PULLBACK | MOMENTUM | REVERSAL | BREAKDOWN | NONE
    ADD COLUMN IF NOT EXISTS entry_price   NUMERIC(14, 4),  -- limit price; BUY rows only
    ADD COLUMN IF NOT EXISTS stop_price    NUMERIC(14, 4),
    ADD COLUMN IF NOT EXISTS target_price  NUMERIC(14, 4),
    ADD COLUMN IF NOT EXISTS horizon_days  INTEGER,         -- planned hold, trading days (1–10)
    ADD COLUMN IF NOT EXISTS risk_reward   NUMERIC(6, 2),   -- (target − entry) / (entry − stop)
    ADD COLUMN IF NOT EXISTS invalidation  TEXT,            -- model's "what proves this wrong"
    ADD COLUMN IF NOT EXISTS atr_14        NUMERIC(14, 4),
    ADD COLUMN IF NOT EXISTS swing_flags   TEXT[],          -- Python corrections, e.g. EARNINGS_BLACKOUT
    ADD COLUMN IF NOT EXISTS swing_notes   TEXT[],          -- human-readable detail of each correction
    ADD COLUMN IF NOT EXISTS model_signal  TEXT;            -- the model's signal before swing_policy

CREATE INDEX IF NOT EXISTS idx_agent_reports_mode ON agent_reports (trading_mode, report_date DESC);

-- One simulated trade per swing BUY plan (swing_backtest.py, Stage 9). Re-simulated
-- in full every run, so OPEN / PENDING rows settle as new bars arrive.
CREATE TABLE IF NOT EXISTS swing_backtest_trades (
    report_run_id    TEXT        NOT NULL,   -- agent_reports.run_id of the plan
    ticker           TEXT        NOT NULL,
    report_date      DATE        NOT NULL,
    signal_strength  TEXT,
    setup_type       TEXT,
    entry_price      NUMERIC(14, 4),
    stop_price       NUMERIC(14, 4),
    target_price     NUMERIC(14, 4),
    horizon_days     INTEGER,
    outcome          TEXT        NOT NULL,   -- TARGET | STOP | TIME | SIGNAL_EXIT | NO_FILL |
                                             -- CANCELLED_GAP | SKIPPED_IN_TRADE | OPEN | PENDING
    fill_date        DATE,
    fill_price       NUMERIC(14, 4),
    exit_date        DATE,
    exit_price       NUMERIC(14, 4),
    hold_sessions    INTEGER,                -- sessions held after the fill day
    return_pct       NUMERIC(10, 4),         -- net of costs
    r_multiple       NUMERIC(8, 3),          -- net P&L / planned risk (entry − stop)
    mfe_r            NUMERIC(8, 2),          -- best excursion while held, in R
    mae_r            NUMERIC(8, 2),          -- worst excursion while held, in R
    backtest_run_id  TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (report_run_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_swing_bt_ticker  ON swing_backtest_trades (ticker, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_swing_bt_outcome ON swing_backtest_trades (outcome);

-- The dashboard reads with the anon key; mirror the other tables' read access.
ALTER TABLE swing_backtest_trades ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "read swing_backtest_trades" ON swing_backtest_trades;
CREATE POLICY "read swing_backtest_trades" ON swing_backtest_trades FOR SELECT TO anon, authenticated USING (true);

NOTIFY pgrst, 'reload schema';
