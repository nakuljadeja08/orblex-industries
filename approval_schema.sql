-- approval_schema.sql — dashboard approvals and kill switch for swing orders.
-- Run once in the Supabase SQL editor, after trading_schema.sql. Re-running is safe.
--
-- The dashboard calls these functions with the signed-in user's Supabase
-- session (magic link, same sign-in as RUN PIPELINE). Each function checks the
-- caller's email against trading_admins and enforces the allowed transition
-- itself, so the browser is never trusted:
--   approve_trade_order   PROPOSED → APPROVED | REJECTED, today's orders only
--   set_trading_halt      flip the kill switch for new entries
-- Nothing here places an order. Approved orders are placed by hand in the
-- broker app; the fill sync records them.

CREATE TABLE IF NOT EXISTS trading_admins (
    email       TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE trading_admins ENABLE ROW LEVEL SECURITY;   -- no policies: unreadable to anon/authenticated
-- Add the emails allowed to approve trades and see the dashboard, once, in the SQL editor:
--   INSERT INTO trading_admins (email) VALUES ('you@example.com') ON CONFLICT DO NOTHING;

ALTER TABLE trade_orders
    ADD COLUMN IF NOT EXISTS approved_by  TEXT,
    ADD COLUMN IF NOT EXISTS approved_at  TIMESTAMPTZ;

CREATE OR REPLACE FUNCTION _is_trading_admin() RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
    SELECT EXISTS (SELECT 1 FROM trading_admins WHERE lower(email) = lower(auth.jwt() ->> 'email'));
$$;

CREATE OR REPLACE FUNCTION approve_trade_order(p_order_id BIGINT, p_approve BOOLEAN)
RETURNS trade_orders
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    o trade_orders;
BEGIN
    IF NOT _is_trading_admin() THEN
        RAISE EXCEPTION 'Not authorised to approve trades.' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO o FROM trade_orders WHERE id = p_order_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Order % not found.', p_order_id;
    END IF;
    IF o.status <> 'PROPOSED' THEN
        RAISE EXCEPTION 'Order % is %; only PROPOSED orders can be approved or rejected.', p_order_id, o.status;
    END IF;
    IF o.plan_date <> (NOW() AT TIME ZONE 'America/New_York')::date THEN
        RAISE EXCEPTION 'Order % is for %, not today — rebuild the plan.', p_order_id, o.plan_date;
    END IF;
    UPDATE trade_orders
       SET status = CASE WHEN p_approve THEN 'APPROVED' ELSE 'REJECTED' END,
           approved_by = auth.jwt() ->> 'email',
           approved_at = NOW(),
           updated_at  = NOW()
     WHERE id = p_order_id
    RETURNING * INTO o;
    RETURN o;
END;
$$;

CREATE OR REPLACE FUNCTION set_trading_halt(p_halted BOOLEAN, p_reason TEXT DEFAULT NULL)
RETURNS trading_controls
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    c trading_controls;
BEGIN
    IF NOT _is_trading_admin() THEN
        RAISE EXCEPTION 'Not authorised to change the kill switch.' USING ERRCODE = '42501';
    END IF;
    UPDATE trading_controls
       SET halted = p_halted,
           halt_reason = CASE WHEN p_halted THEN coalesce(nullif(p_reason, ''), 'halted from dashboard by ' || (auth.jwt() ->> 'email')) END,
           updated_at = NOW()
     WHERE id = 1
    RETURNING * INTO c;
    RETURN c;
END;
$$;

REVOKE ALL ON FUNCTION _is_trading_admin()                  FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION approve_trade_order(BIGINT, BOOLEAN) FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION set_trading_halt(BOOLEAN, TEXT)      FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION _is_trading_admin()                  TO authenticated;
GRANT EXECUTE ON FUNCTION approve_trade_order(BIGINT, BOOLEAN) TO authenticated;
GRANT EXECUTE ON FUNCTION set_trading_halt(BOOLEAN, TEXT)      TO authenticated;

NOTIFY pgrst, 'reload schema';
