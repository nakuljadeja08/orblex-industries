-- trading_schema.sql — order ledger, fill ledger and kill switch for swing
-- execution (trade_plan.py / trade_guard.py / run_trade_plan.py).
-- Run once in the Supabase SQL editor. Re-running is safe.

-- Every order the plan proposes, with its guard result and lifecycle.
-- status: PROPOSED → APPROVED (user said yes) → PLACED → FILLED | PARTIAL | CANCELLED
--         BLOCKED (guard said no) | REJECTED (user said no) | EXPIRED (entry window passed)
CREATE TABLE IF NOT EXISTS trade_orders (
    id               BIGSERIAL PRIMARY KEY,
    plan_id          TEXT        NOT NULL,   -- one run of run_trade_plan.py plan
    plan_date        DATE        NOT NULL,   -- ET trading date the order is for
    ticker           TEXT        NOT NULL,
    side             TEXT        NOT NULL,   -- BUY | SELL
    intent           TEXT        NOT NULL,   -- ENTRY | EXIT_SIGNAL | EXIT_STOP | EXIT_TARGET | EXIT_TIME | PROTECT_STOP
    order_type       TEXT        NOT NULL,   -- LIMIT | MARKET | STOP
    time_in_force    TEXT,                   -- DAY | GTC
    qty              NUMERIC(14, 4) NOT NULL,
    limit_price      NUMERIC(14, 4),
    stop_price       NUMERIC(14, 4),         -- entry: plan stop; PROTECT_STOP: trigger price
    target_price     NUMERIC(14, 4),
    horizon_days     INTEGER,
    risk_dollars     NUMERIC(14, 2),         -- qty × (entry − stop)
    report_run_id    TEXT,                   -- agent_reports.run_id of the plan behind it
    report_date      DATE,
    signal_strength  TEXT,
    setup_type       TEXT,
    cancel_first     TEXT[],                 -- broker order ids to cancel before placing
    status           TEXT        NOT NULL,
    guard_reasons    TEXT[],
    note             TEXT,
    broker_order_id  TEXT,
    account_source   TEXT,                   -- robinhood | paper
    checked_at       TIMESTAMPTZ,            -- last pre-placement guard pass
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trade_orders_date   ON trade_orders (plan_date DESC, ticker);
CREATE INDEX IF NOT EXISTS idx_trade_orders_status ON trade_orders (status);

-- Every execution, from the broker. The same-day rules read this as well as the
-- broker's own fills, so either source alone is enough to block a day trade.
CREATE TABLE IF NOT EXISTS trade_fills (
    id               BIGSERIAL PRIMARY KEY,
    order_id         BIGINT REFERENCES trade_orders (id),
    ticker           TEXT        NOT NULL,
    side             TEXT        NOT NULL,   -- BUY | SELL
    qty              NUMERIC(14, 4) NOT NULL,
    price            NUMERIC(14, 4) NOT NULL,
    filled_at        TIMESTAMPTZ NOT NULL,
    trade_date       DATE        NOT NULL,   -- ET date of filled_at
    broker_order_id  TEXT,
    source           TEXT,                   -- robinhood | paper | manual
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trade_fills_date ON trade_fills (trade_date DESC, ticker);

-- Single-row kill switch and equity high-water mark (id = 1).
CREATE TABLE IF NOT EXISTS trading_controls (
    id           INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    halted       BOOLEAN     NOT NULL DEFAULT FALSE,
    halt_reason  TEXT,
    equity_peak  NUMERIC(14, 2),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO trading_controls (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- Admins read these through the signed-in dashboard: private_access_schema.sql.
ALTER TABLE trade_orders     ENABLE ROW LEVEL SECURITY;
ALTER TABLE trade_fills      ENABLE ROW LEVEL SECURITY;
ALTER TABLE trading_controls ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "read trade_orders"     ON trade_orders;
DROP POLICY IF EXISTS "read trade_fills"      ON trade_fills;
DROP POLICY IF EXISTS "read trading_controls" ON trading_controls;

NOTIFY pgrst, 'reload schema';
