-- ============================================================
-- Orblex Industries — Pipeline Metrics Schema
-- Run this in your Supabase SQL Editor
-- Adds: pipeline_metrics table for operational observability
-- ============================================================

-- Stage-level execution record written by run_all.py after every stage
CREATE TABLE IF NOT EXISTS pipeline_metrics (
    id               BIGSERIAL PRIMARY KEY,
    pipeline_run_id  TEXT        NOT NULL,
    run_date         DATE        NOT NULL,
    stage_name       TEXT        NOT NULL,   -- "macro" | "earnings" | "pipeline" | "options" | "analyst" | "risk" | "allocator" | "report" | "watchdog" | "newsletter"
    status           TEXT        NOT NULL DEFAULT 'ok',   -- "ok" | "failed" | "skipped"
    duration_seconds NUMERIC(8, 2),          -- wall-clock seconds the stage took
    token_cost       INTEGER,                -- total tokens (prompt + output) consumed, NULL for stages with no LLM calls
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(pipeline_run_id, stage_name)
);

CREATE INDEX IF NOT EXISTS idx_pm_pipeline_run_id ON pipeline_metrics(pipeline_run_id);
CREATE INDEX IF NOT EXISTS idx_pm_run_date        ON pipeline_metrics(run_date DESC);
CREATE INDEX IF NOT EXISTS idx_pm_stage_name      ON pipeline_metrics(stage_name);
