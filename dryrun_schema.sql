-- dryrun_schema.sql — phase 4: what the executor WOULD have done each morning against the
-- live account (run_dryrun.py), for comparison with what the user actually placed.
-- Run once in the Supabase SQL editor. Re-running is safe. Nothing here places an order.

CREATE TABLE IF NOT EXISTS exec_dryrun (
    id               BIGSERIAL PRIMARY KEY,
    run_date         DATE        NOT NULL,           -- ET trading date
    run_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status           TEXT        NOT NULL,           -- OK | HALTED | STOP_FILE | ERROR
    mandate_source   TEXT,                           -- DASHBOARD (the real one) | ASSUMED (none was set)
    would            JSONB       NOT NULL DEFAULT '[]',   -- [{action, ticker, side, type, qty, limit, stop, ...}]
    refusals         JSONB       NOT NULL DEFAULT '[]',   -- [{order_id, ticker, intent, reasons}]
    problems         TEXT[],                          -- reconcile problems (would have halted)
    alerts           TEXT[],
    comparison       JSONB,                           -- filled in after the close by compare_dryrun.py
    UNIQUE (run_date, run_at)
);
CREATE INDEX IF NOT EXISTS idx_exec_dryrun_date ON exec_dryrun (run_date DESC);
-- Admins read it through the signed-in dashboard: private_access_schema.sql.
ALTER TABLE exec_dryrun ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "read exec_dryrun" ON exec_dryrun;

NOTIFY pgrst, 'reload schema';
