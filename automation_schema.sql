-- automation_schema.sql — run the trading jobs on time without the PC or Claude:
-- Supabase pg_cron decides (in America/New_York) which jobs are due and dispatches the
-- GitHub Actions workflows; the jobs (jobs.py) record every run here and email alerts.
-- Run once in the Supabase SQL editor, after approval_schema.sql. Re-running is safe.
--
-- ONE-TIME, by you (never commit the token):
--   1. Database → Extensions: enable pg_cron and pg_net.
--   2. Create a fine-grained GitHub token for your repository with
--      "Actions: Read and write" only, then in the SQL editor:
--        select vault.create_secret('<the token>', 'github_dispatch_token');
--        select vault.create_secret('<owner>/<repo>', 'github_repo');
--   3. Run this file.

-- ── Robinhood sign-in, encrypted (rh_client.SupabaseStore) ─────────────────────
-- Ciphertext only: the key (RH_TOKEN_KEY) is in GitHub Secrets and your local .env,
-- never in the database. No RLS policy at all: only the service key can read it.
CREATE TABLE IF NOT EXISTS broker_secrets (
    name        TEXT PRIMARY KEY,
    ciphertext  TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE broker_secrets ENABLE ROW LEVEL SECURITY;

-- ── Job runs (the dashboard's AUTOMATION tab) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS job_runs (
    id            BIGSERIAL PRIMARY KEY,
    job           TEXT        NOT NULL,              -- morning_plan | dryrun | fill_sync | eod_review | holdings_snapshot
    run_date      DATE        NOT NULL,              -- ET date
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    status        TEXT        NOT NULL DEFAULT 'RUNNING',   -- RUNNING | OK | WARN | FAILED | SKIPPED
    summary       TEXT,
    details       JSONB,
    gh_run_url    TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs_date ON job_runs (run_date DESC, job);
-- Admins read it through the signed-in dashboard: private_access_schema.sql.
ALTER TABLE job_runs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "read job_runs" ON job_runs;

-- ── Daily review (the dashboard's DAILY REVIEW tab) ────────────────────────────
-- Holds account values, so admins only (signed-in dashboard), never anon.
CREATE TABLE IF NOT EXISTS daily_reviews (
    run_date    DATE PRIMARY KEY,
    review      JSONB       NOT NULL,
    summary     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE daily_reviews ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "admins read daily_reviews" ON daily_reviews;
CREATE POLICY "admins read daily_reviews" ON daily_reviews FOR SELECT TO authenticated USING (_is_trading_admin());

-- ── The schedule, in ET ────────────────────────────────────────────────────────
-- One row per dispatch. `workflow` is the file in .github/workflows; `inputs` are its
-- workflow_dispatch inputs. Days use ISO weekday numbers (1 = Monday … 7 = Sunday).
CREATE TABLE IF NOT EXISTS automation_schedule (
    name      TEXT PRIMARY KEY,
    workflow  TEXT    NOT NULL,
    inputs    JSONB   NOT NULL DEFAULT '{}',
    et_time   TIME    NOT NULL,
    days      INT[]   NOT NULL DEFAULT '{1,2,3,4,5}',
    grace_min INT     NOT NULL DEFAULT 45,        -- still dispatched this late (e.g. after an outage)
    enabled   BOOLEAN NOT NULL DEFAULT TRUE
);
ALTER TABLE automation_schedule ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "read automation_schedule" ON automation_schedule;
CREATE POLICY "read automation_schedule" ON automation_schedule FOR SELECT TO anon, authenticated USING (true);

INSERT INTO automation_schedule (name, workflow, inputs, et_time, days) VALUES
    ('pipeline',          'orblex_pipeline.yml', '{"source": "cron"}',                 '06:30', '{1,2,3,4,5}'),
    ('morning_plan',      'trading_jobs.yml',   '{"job": "morning_plan"}',           '08:40', '{1,2,3,4,5}'),
    ('dryrun',            'trading_jobs.yml',   '{"job": "dryrun"}',                 '09:25', '{1,2,3,4,5}'),
    ('fill_sync_1020',    'trading_jobs.yml',   '{"job": "fill_sync"}',              '10:20', '{1,2,3,4,5}'),
    ('fill_sync_1320',    'trading_jobs.yml',   '{"job": "fill_sync"}',              '13:20', '{1,2,3,4,5}'),
    ('fill_sync_1620',    'trading_jobs.yml',   '{"job": "fill_sync"}',              '16:20', '{1,2,3,4,5}'),
    ('eod_review',        'trading_jobs.yml',   '{"job": "eod_review"}',             '16:35', '{1,2,3,4,5}'),
    ('holdings_snapshot', 'trading_jobs.yml',   '{"job": "holdings_snapshot"}',      '08:30', '{6}'),
    ('longterm_run',      'orblex_pipeline.yml', '{"source": "cron", "book": "longterm"}', '09:00', '{6}')
ON CONFLICT (name) DO NOTHING;

-- Later live-executor passes, each just after a fill sync: approvals that came after 9:25, and
-- protective stops on today's fills. Off until the live switch: when you set EXECUTOR_LIVE=1, run
--   UPDATE automation_schedule SET enabled = TRUE WHERE name LIKE 'executor_pass%';
INSERT INTO automation_schedule (name, workflow, inputs, et_time, days, enabled) VALUES
    ('executor_pass_1030', 'trading_jobs.yml', '{"job": "executor_pass"}', '10:30', '{1,2,3,4,5}', FALSE),
    ('executor_pass_1330', 'trading_jobs.yml', '{"job": "executor_pass"}', '13:30', '{1,2,3,4,5}', FALSE),
    ('executor_pass_1530', 'trading_jobs.yml', '{"job": "executor_pass"}', '15:30', '{1,2,3,4,5}', FALSE)
ON CONFLICT (name) DO NOTHING;

-- Every dispatch attempt, at most one success per job per ET day.
CREATE TABLE IF NOT EXISTS automation_dispatches (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT        NOT NULL,
    run_date     DATE        NOT NULL,
    dispatched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    request_id   BIGINT,                  -- pg_net request id (net._http_response.id)
    UNIQUE (name, run_date)
);
ALTER TABLE automation_dispatches ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "read automation_dispatches" ON automation_dispatches;
CREATE POLICY "read automation_dispatches" ON automation_dispatches FOR SELECT TO anon, authenticated USING (true);

-- Called every 5 minutes by pg_cron. Market holidays are handled by the jobs themselves
-- (they check the NYSE calendar and record SKIPPED).
CREATE OR REPLACE FUNCTION dispatch_due_jobs() RETURNS INT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE
    now_et  TIMESTAMP := NOW() AT TIME ZONE 'America/New_York';
    today   DATE := now_et::date;
    s       automation_schedule;
    token   TEXT;
    repo    TEXT;
    req     BIGINT;
    n       INT := 0;
BEGIN
    SELECT decrypted_secret INTO token FROM vault.decrypted_secrets WHERE name = 'github_dispatch_token';
    SELECT decrypted_secret INTO repo FROM vault.decrypted_secrets WHERE name = 'github_repo';
    IF token IS NULL OR repo IS NULL THEN
        RAISE WARNING 'dispatch_due_jobs: vault secret github_dispatch_token or github_repo is missing';
        RETURN 0;
    END IF;
    FOR s IN SELECT * FROM automation_schedule
             WHERE enabled
               AND EXTRACT(ISODOW FROM now_et)::int = ANY (days)
               AND now_et::time >= et_time
               AND now_et::time <  et_time + make_interval(mins => grace_min)
    LOOP
        BEGIN
            INSERT INTO automation_dispatches (name, run_date) VALUES (s.name, today);
        EXCEPTION WHEN unique_violation THEN
            CONTINUE;                                   -- already dispatched today
        END;
        SELECT net.http_post(
            url := 'https://api.github.com/repos/' || repo || '/actions/workflows/'
                   || s.workflow || '/dispatches',
            body := jsonb_build_object('ref', 'release', 'inputs', s.inputs),
            headers := jsonb_build_object(
                'Authorization', 'Bearer ' || token,
                'Accept', 'application/vnd.github+json',
                'X-GitHub-Api-Version', '2022-11-28',
                'User-Agent', 'orblex-industries-dispatcher',
                'Content-Type', 'application/json')
        ) INTO req;
        UPDATE automation_dispatches SET request_id = req WHERE name = s.name AND run_date = today;
        n := n + 1;
    END LOOP;
    RETURN n;
END $$;
REVOKE ALL ON FUNCTION dispatch_due_jobs() FROM PUBLIC, anon, authenticated;

-- Every 5 minutes, all week (the function filters by ET day and time).
SELECT cron.unschedule('orblex-dispatch') WHERE EXISTS (SELECT 1 FROM cron.job WHERE jobname = 'orblex-dispatch');
SELECT cron.schedule('orblex-dispatch', '*/5 * * * *', $$SELECT public.dispatch_due_jobs()$$);

NOTIFY pgrst, 'reload schema';
