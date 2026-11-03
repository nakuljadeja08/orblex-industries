-- private_access_schema.sql — the whole database is readable by signed-in trading admins only.
-- Run in the Supabase SQL editor AFTER the dashboard version with the login panel (LoginGate.jsx)
-- is live, and again after running any other schema file. Re-running is safe.
--
-- The dashboard's public key ships in its JavaScript, so a policy open to anon means open to
-- anyone. That covers more than the account tables: the daily plans and the model's reasoning show
-- what you are about to trade. The public demo has its own build and sample data, and never reads this
-- database. The pipeline and jobs use the service key, which bypasses all of this, so they are unaffected.

-- 1. The public key can't touch any table or view, now or in future, whatever a policy says.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon;

DO $$
DECLARE
    r RECORD;
BEGIN
    -- 2. Replace every policy with one rule: signed-in trading admins may read. Writes from the
    --    browser only happen through the SECURITY DEFINER functions, which check the admin themselves.
    FOR r IN SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'public' LOOP
        EXECUTE format('DROP POLICY %I ON %I', r.policyname, r.tablename);
    END LOOP;
    FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', r.tablename);
        -- These three stay unreadable from any browser: service key only.
        IF r.tablename NOT IN ('broker_secrets', 'trading_admins', 'account_snapshots') THEN
            EXECUTE format('CREATE POLICY %I ON %I FOR SELECT TO authenticated USING (_is_trading_admin())',
                           'admins read ' || r.tablename, r.tablename);
        END IF;
    END LOOP;
    -- 3. A view normally reads as its owner, which skips row-level security. Make views obey it.
    FOR r IN SELECT viewname FROM pg_views WHERE schemaname = 'public' LOOP
        EXECUTE format('ALTER VIEW %I SET (security_invoker = true)', r.viewname);
    END LOOP;
END $$;

NOTIFY pgrst, 'reload schema';
