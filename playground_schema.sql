-- playground_schema.sql — the public playground's accounts and saved runs.
--
-- Run this in a SEPARATE Supabase project, never the trading one: playground visitors sign up freely,
-- and a signed-in visitor must never share a database with your trading data.
-- Run once in that project's SQL editor. Re-running is safe.
--
-- Then, in that project:
--   Authentication → Sign In / Providers → Email: on, with "Confirm email" on
--   Authentication → URL Configuration: add the demo's address as the Site URL and a redirect URL
--   Authentication → Attack Protection: turn on CAPTCHA if sign-ups get abused
--
-- Visitors' Claude keys are never stored here, or anywhere: api/playground.py uses each one for a
-- single run.

CREATE TABLE IF NOT EXISTS playground_runs (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID        NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    scenario    JSONB       NOT NULL,
    result      JSONB       NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_playground_runs_user ON playground_runs (user_id, created_at DESC);

-- Each visitor sees and deletes only their own runs. Only the server function (service key) writes.
ALTER TABLE playground_runs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON playground_runs FROM anon;
REVOKE INSERT, UPDATE ON playground_runs FROM authenticated;
DROP POLICY IF EXISTS "read own runs" ON playground_runs;
CREATE POLICY "read own runs" ON playground_runs FOR SELECT TO authenticated USING (user_id = auth.uid());
DROP POLICY IF EXISTS "delete own runs" ON playground_runs;
CREATE POLICY "delete own runs" ON playground_runs FOR DELETE TO authenticated USING (user_id = auth.uid());

-- A visitor can delete their own account, which removes their runs too (ON DELETE CASCADE).
CREATE OR REPLACE FUNCTION delete_my_account() RETURNS VOID
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
    DELETE FROM auth.users WHERE id = auth.uid();
$$;
REVOKE ALL ON FUNCTION delete_my_account() FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION delete_my_account() TO authenticated;

NOTIFY pgrst, 'reload schema';
