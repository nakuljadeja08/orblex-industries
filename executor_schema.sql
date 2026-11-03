-- executor_schema.sql — the daily mandate and the execution log (EXECUTOR_DESIGN.md §1.3, phase 2).
-- Run once in the Supabase SQL editor, after trading_schema.sql and approval_schema.sql
-- (it uses trade_orders and _is_trading_admin). Re-running is safe.
-- Nothing here places an order.

-- ── Daily mandate (layer 2) ──────────────────────────────────────────────────
-- One row per ET trading day: what the user allowed the executor to enter that day.
-- No row, or a REVOKED / expired one → the executor manages exits only.
-- The database clamps every field to the code ceilings (exec_limits.py) so a mandate
-- can only ever be tighter than the ceilings, whatever the browser sends.
CREATE TABLE IF NOT EXISTS trading_mandates (
    mandate_date         DATE PRIMARY KEY,                 -- ET trading date
    max_entries          INTEGER     NOT NULL CHECK (max_entries BETWEEN 0 AND 2),
    max_total_risk_usd   NUMERIC(12, 2) NOT NULL CHECK (max_total_risk_usd >= 0),
    allowed_tickers      TEXT[]      NOT NULL DEFAULT '{}',
    order_types          TEXT[]      NOT NULL DEFAULT '{LIMIT}' CHECK (order_types <@ ARRAY['LIMIT']),
    require_stop         BOOLEAN     NOT NULL DEFAULT TRUE CHECK (require_stop),
    allow_same_day_exit  BOOLEAN     NOT NULL DEFAULT FALSE CHECK (NOT allow_same_day_exit),  -- unused: same-day exits are set in exec_limits.py, not per mandate
    expires_at           TIMESTAMPTZ NOT NULL,
    status               TEXT        NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'REVOKED')),
    approved_by          TEXT,
    approved_at          TIMESTAMPTZ,
    revoked_at           TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Admins read it through the signed-in dashboard: private_access_schema.sql.
ALTER TABLE trading_mandates ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "read trading_mandates" ON trading_mandates;

-- Approve (or replace) today's mandate. Admin only; tickers upper-cased and de-duplicated.
-- Expires at 16:00 ET today. Replacing an ACTIVE mandate is allowed (the newer one wins).
CREATE OR REPLACE FUNCTION set_trading_mandate(p_max_entries INTEGER, p_max_total_risk_usd NUMERIC,
                                               p_tickers TEXT[])
RETURNS trading_mandates
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    today DATE := (NOW() AT TIME ZONE 'America/New_York')::date;
    m trading_mandates;
BEGIN
    IF NOT _is_trading_admin() THEN
        RAISE EXCEPTION 'Not authorised to set the trading mandate.' USING ERRCODE = '42501';
    END IF;
    IF p_max_entries IS NULL OR p_max_entries < 0 OR p_max_entries > 2 THEN
        RAISE EXCEPTION 'max_entries must be 0, 1 or 2.';
    END IF;
    IF p_max_total_risk_usd IS NULL OR p_max_total_risk_usd < 0 THEN
        RAISE EXCEPTION 'max_total_risk_usd must be zero or more.';
    END IF;
    INSERT INTO trading_mandates (mandate_date, max_entries, max_total_risk_usd, allowed_tickers,
                                  expires_at, status, approved_by, approved_at, revoked_at)
    VALUES (today, p_max_entries, round(p_max_total_risk_usd, 2),
            ARRAY(SELECT DISTINCT upper(trim(t)) FROM unnest(COALESCE(p_tickers, '{}')) t WHERE trim(t) <> ''),
            (today + TIME '16:00') AT TIME ZONE 'America/New_York',
            'ACTIVE', auth.jwt() ->> 'email', NOW(), NULL)
    ON CONFLICT (mandate_date) DO UPDATE SET
        max_entries = EXCLUDED.max_entries, max_total_risk_usd = EXCLUDED.max_total_risk_usd,
        allowed_tickers = EXCLUDED.allowed_tickers, expires_at = EXCLUDED.expires_at,
        status = 'ACTIVE', approved_by = EXCLUDED.approved_by, approved_at = NOW(), revoked_at = NULL
    RETURNING * INTO m;
    RETURN m;
END $$;

CREATE OR REPLACE FUNCTION revoke_trading_mandate()
RETURNS trading_mandates
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    today DATE := (NOW() AT TIME ZONE 'America/New_York')::date;
    m trading_mandates;
BEGIN
    IF NOT _is_trading_admin() THEN
        RAISE EXCEPTION 'Not authorised to revoke the trading mandate.' USING ERRCODE = '42501';
    END IF;
    UPDATE trading_mandates SET status = 'REVOKED', revoked_at = NOW()
     WHERE mandate_date = today RETURNING * INTO m;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'There is no mandate for today.';
    END IF;
    RETURN m;
END $$;

REVOKE ALL ON FUNCTION set_trading_mandate(INTEGER, NUMERIC, TEXT[]) FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION revoke_trading_mandate() FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION set_trading_mandate(INTEGER, NUMERIC, TEXT[]) TO authenticated;
GRANT EXECUTE ON FUNCTION revoke_trading_mandate() TO authenticated;

-- ── Execution log ────────────────────────────────────────────────────────────
-- Every submit / retry / cancel / replace the executor makes (phase 3+). The rate
-- ceilings in exec_limits.py count from here; the manual flow never writes it.
CREATE TABLE IF NOT EXISTS exec_events (
    id               BIGSERIAL PRIMARY KEY,
    order_id         BIGINT      REFERENCES trade_orders (id),
    kind             TEXT        NOT NULL CHECK (kind IN ('SUBMIT', 'RESUBMIT', 'CANCEL', 'REPLACE')),
    ticker           TEXT        NOT NULL,
    intent           TEXT        NOT NULL,
    risk_dollars     NUMERIC(12, 2),
    client_order_id  TEXT,                    -- wi-<trade_orders.id>: idempotent at the broker
    broker_order_id  TEXT,
    ok               BOOLEAN     NOT NULL,    -- the broker accepted it
    detail           TEXT,
    at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    trade_date       DATE        NOT NULL     -- ET date of `at`
);
CREATE INDEX IF NOT EXISTS idx_exec_events_date ON exec_events (trade_date DESC, order_id);
ALTER TABLE exec_events ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "read exec_events" ON exec_events;

NOTIFY pgrst, 'reload schema';
