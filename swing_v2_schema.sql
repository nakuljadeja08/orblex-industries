-- swing_v2_schema.sql — PIPELINE_V2 shadow picks (run_swing_v2.py), compared with v1 plans
-- for two weeks before v2 may feed trade_plan (EXECUTOR_DESIGN.md phase 1).
-- Run once in the Supabase SQL editor. Re-running is safe.

-- One row per screened candidate per run.
-- status: PICK      top KEEP candidates, within the 2 entries/day limit
--         RESERVE   KEEP, but past the daily limit
--         VETO      the model (or its news read) said no
--         BLOCKED   a swing_policy rule said no (stale data, earnings, reward:risk)
--         DROPPED   past the candidate cap, never sent to the model
-- script_pick marks what a script-only v2 would have bought (top 2 by script rank,
-- ignoring the model), so one table answers "does the model's veto add anything?".
CREATE TABLE IF NOT EXISTS swing_v2_picks (
    id                BIGSERIAL PRIMARY KEY,
    run_id            TEXT        NOT NULL,
    report_date       DATE        NOT NULL,
    ticker            TEXT        NOT NULL,
    setup_type        TEXT        NOT NULL,
    regime            TEXT,
    entry_price       NUMERIC(14, 4),
    stop_price        NUMERIC(14, 4),
    target_price      NUMERIC(14, 4),
    horizon_days      INTEGER,
    risk_mult         NUMERIC(6, 3),
    risk_reward       NUMERIC(8, 2),
    rank_score        NUMERIC(10, 3),
    script_rank       INTEGER,
    script_pick       BOOLEAN     NOT NULL DEFAULT FALSE,
    news_catalyst     TEXT,
    news_reason       TEXT,
    llm_decision      TEXT,
    llm_rank          INTEGER,
    llm_reason        TEXT,
    llm_stop_from     NUMERIC(14, 4),        -- the script's stop when the model tightened it
    llm_status        TEXT,                  -- OK | SKIPPED | ERROR: <message>
    status            TEXT        NOT NULL,
    flags             TEXT[],
    notes             TEXT[],
    tokens            INTEGER,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_swing_v2_picks_date ON swing_v2_picks (report_date DESC, status);

ALTER TABLE swing_v2_picks ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "read swing_v2_picks" ON swing_v2_picks;
CREATE POLICY "read swing_v2_picks" ON swing_v2_picks FOR SELECT TO anon, authenticated USING (true);
