-- ============================================================
-- Orblex Industries — Agent Layer Schema v2
-- Run this in your Supabase SQL Editor
-- Adds: Risk Manager, Allocator, Report Writer, Watchdog tables
-- ============================================================


-- ── RISK MANAGER AGENT ────────────────────────────────────────────────────────

-- Portfolio-level risk assessment — one row per risk run
CREATE TABLE IF NOT EXISTS risk_assessments (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              TEXT        NOT NULL UNIQUE,
    assessment_date     DATE        NOT NULL,
    analyst_run_id      TEXT,                       -- links back to agent_reports run
    total_tickers       INTEGER,
    buy_count           INTEGER,
    sell_count          INTEGER,
    hold_count          INTEGER,
    market_risk_level   TEXT,                       -- 'LOW' | 'MODERATE' | 'HIGH' | 'CRITICAL'
    concentration_risk  TEXT,                       -- 'LOW' | 'MODERATE' | 'HIGH'
    volatility_risk     TEXT,                       -- 'LOW' | 'MODERATE' | 'HIGH'
    flagged_tickers     TEXT[],                     -- tickers with risk_score >= 7
    position_limits     JSONB,                      -- { "AAPL": 25.0, "TSLA": 8.0, ... }
    risk_summary        TEXT,
    reasoning           TEXT,
    model               TEXT,
    prompt_tokens       INTEGER,
    output_tokens       INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-ticker risk details — one row per ticker per risk run
CREATE TABLE IF NOT EXISTS risk_ticker_details (
    id                  BIGSERIAL PRIMARY KEY,
    assessment_run_id   TEXT        NOT NULL,       -- FK → risk_assessments.run_id
    ticker              TEXT        NOT NULL,
    analyst_signal      TEXT,                       -- copied from agent_reports
    signal_strength     TEXT,
    volatility          NUMERIC(8, 4),              -- annualised volatility
    anomaly_count       INTEGER     DEFAULT 0,
    risk_score          INTEGER,                    -- 1 (safe) to 10 (dangerous)
    max_position_pct    NUMERIC(5, 2),              -- max % of portfolio
    risk_flags          TEXT[],                     -- e.g. ['HIGH_VOLATILITY', 'OVERBOUGHT']
    notes               TEXT,

    UNIQUE (assessment_run_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_risk_assessments_date        ON risk_assessments (assessment_date DESC);
CREATE INDEX IF NOT EXISTS idx_risk_assessments_risk_level  ON risk_assessments (market_risk_level);
CREATE INDEX IF NOT EXISTS idx_risk_ticker_details_run      ON risk_ticker_details (assessment_run_id);
CREATE INDEX IF NOT EXISTS idx_risk_ticker_details_ticker   ON risk_ticker_details (ticker);


-- ── PORTFOLIO ALLOCATOR AGENT ─────────────────────────────────────────────────

-- Portfolio-level allocation plan — one row per allocator run
CREATE TABLE IF NOT EXISTS allocation_plans (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              TEXT        NOT NULL UNIQUE,
    plan_date           DATE        NOT NULL,
    analyst_run_id      TEXT,                       -- FK → agent_run_log.run_id
    risk_run_id         TEXT,                       -- FK → risk_assessments.run_id
    total_tickers       INTEGER,
    invested_pct        NUMERIC(5, 2),              -- total % deployed
    cash_pct            NUMERIC(5, 2),              -- % held as cash
    strategy_note       TEXT,                       -- allocator's strategy explanation
    allocations         JSONB,                      -- { "AAPL": 28.5, "TSLA": 0.0, ... }
    model               TEXT,
    prompt_tokens       INTEGER,
    output_tokens       INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-ticker allocation details — one row per ticker per allocator run
CREATE TABLE IF NOT EXISTS allocation_details (
    id                      BIGSERIAL PRIMARY KEY,
    plan_run_id             TEXT        NOT NULL,   -- FK → allocation_plans.run_id
    ticker                  TEXT        NOT NULL,
    analyst_signal          TEXT,
    signal_strength         TEXT,
    risk_score              INTEGER,
    max_position_pct        NUMERIC(5, 2),
    risk_flags              TEXT[],
    allocated_pct           NUMERIC(5, 2),
    allocation_rationale    TEXT,
    sizing_method           TEXT,                   -- 'SIGNAL_WEIGHTED' | 'RISK_CAPPED' | 'EXCLUDED' | 'MINIMUM_HOLD'

    UNIQUE (plan_run_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_allocation_plans_date        ON allocation_plans (plan_date DESC);
CREATE INDEX IF NOT EXISTS idx_allocation_plans_risk_run    ON allocation_plans (risk_run_id);
CREATE INDEX IF NOT EXISTS idx_allocation_details_run       ON allocation_details (plan_run_id);
CREATE INDEX IF NOT EXISTS idx_allocation_details_ticker    ON allocation_details (ticker);

-- Migrations: sector / theme exposure columns (run once against existing tables)
ALTER TABLE allocation_plans    ADD COLUMN IF NOT EXISTS sector_exposure  JSONB;
ALTER TABLE allocation_plans    ADD COLUMN IF NOT EXISTS theme_exposure   JSONB;
ALTER TABLE allocation_details  ADD COLUMN IF NOT EXISTS sector           TEXT;

-- Post-LLM constraint enforcer output (instrument_policy.enforce_allocation_constraints).
-- Semantics matter downstream:
--   NULL  → the enforcer did not run for this plan (pre-fix rows, or a bug)
--   '[]'  → the enforcer ran and the model's allocation was already compliant
--   [...] → the enforcer ran and corrected the listed issues
-- Must have NO default: a default of '[]' would make every un-enforced row claim
-- it was checked and clean, which is the exact false all-clear this column exists
-- to eliminate.
ALTER TABLE allocation_plans    ADD COLUMN IF NOT EXISTS constraint_violations JSONB;
ALTER TABLE allocation_plans    ALTER COLUMN constraint_violations DROP DEFAULT;

-- Per-ticker deterministic base weight (signal x strength, risk-capped and
-- re-normalised) computed by _compute_base_weights() BEFORE the model refines.
-- Persisting it lets the dashboard show the full decision trace — the base the
-- Python code handed the model vs the final weight after the model + enforcer.
-- NULL default: pre-fix rows never recorded a base weight; they read as unknown
-- rather than being backfilled with a fabricated number.
ALTER TABLE allocation_details  ADD COLUMN IF NOT EXISTS base_weight NUMERIC(5, 2);


-- ── REPORT WRITER AGENT ───────────────────────────────────────────────────────

-- Final synthesized report — one row per report run
CREATE TABLE IF NOT EXISTS final_reports (
    id                      BIGSERIAL PRIMARY KEY,
    run_id                  TEXT        NOT NULL UNIQUE,
    report_date             DATE        NOT NULL,
    alloc_run_id            TEXT,                   -- FK → allocation_plans.run_id
    risk_run_id             TEXT,                   -- FK → risk_assessments.run_id
    analyst_run_id          TEXT,                   -- FK → agent_run_log.run_id
    market_risk_level       TEXT,
    invested_pct            NUMERIC(5, 2),
    cash_pct                NUMERIC(5, 2),
    portfolio_stance        TEXT,                   -- 'AGGRESSIVE' | 'BALANCED' | 'DEFENSIVE' | 'CASH_HEAVY'
    confidence_level        TEXT,                   -- 'HIGH' | 'MODERATE' | 'LOW'
    confidence_reasoning    TEXT,
    executive_summary       TEXT,
    market_outlook          TEXT,
    key_themes              TEXT[],
    risk_highlights         TEXT,
    anomaly_digest          TEXT,
    watchdog_notes          TEXT,                   -- pre-audit notes flagged by Report Writer
    disclaimer              TEXT,
    model                   TEXT,
    prompt_tokens           INTEGER,
    output_tokens           INTEGER,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-ticker narratives — one row per ticker per report run
CREATE TABLE IF NOT EXISTS final_report_tickers (
    id                  BIGSERIAL PRIMARY KEY,
    report_run_id       TEXT        NOT NULL,       -- FK → final_reports.run_id
    ticker              TEXT        NOT NULL,
    one_liner           TEXT,                       -- single sentence summary
    full_narrative      TEXT,                       -- 2-3 sentence deep dive

    UNIQUE (report_run_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_final_reports_date           ON final_reports (report_date DESC);
CREATE INDEX IF NOT EXISTS idx_final_reports_stance         ON final_reports (portfolio_stance);
CREATE INDEX IF NOT EXISTS idx_final_reports_confidence     ON final_reports (confidence_level);
CREATE INDEX IF NOT EXISTS idx_final_report_tickers_run     ON final_report_tickers (report_run_id);
CREATE INDEX IF NOT EXISTS idx_final_report_tickers_ticker  ON final_report_tickers (ticker);


-- ── WATCHDOG AGENT ────────────────────────────────────────────────────────────

-- Watchdog audit log — one row per watchdog run
CREATE TABLE IF NOT EXISTS watchdog_logs (
    id                          BIGSERIAL PRIMARY KEY,
    run_id                      TEXT        NOT NULL UNIQUE,
    watchdog_date               DATE        NOT NULL,
    report_run_id               TEXT,               -- FK → final_reports.run_id
    alloc_run_id                TEXT,               -- FK → allocation_plans.run_id
    risk_run_id                 TEXT,               -- FK → risk_assessments.run_id
    analyst_run_id              TEXT,               -- FK → agent_run_log.run_id
    overall_status              TEXT,               -- 'CLEAN' | 'WARNINGS' | 'VIOLATIONS'
    pipeline_health_score       INTEGER,            -- 1 (broken) to 10 (clean)
    audit_summary               TEXT,
    signal_quality_assessment   TEXT,
    rule_check_results          JSONB,              -- array of {rule, status, finding}
    recommendations             TEXT[],
    model                       TEXT,
    prompt_tokens               INTEGER,
    output_tokens               INTEGER,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Individual issues flagged by the Watchdog — one row per issue per run
CREATE TABLE IF NOT EXISTS watchdog_issues (
    id                  BIGSERIAL PRIMARY KEY,
    watchdog_run_id     TEXT        NOT NULL,       -- FK → watchdog_logs.run_id
    issue_type          TEXT,                       -- 'SIGNAL_ALLOCATION_MISMATCH' | 'CAP_VIOLATION' | etc.
    severity            TEXT,                       -- 'LOW' | 'MEDIUM' | 'HIGH'
    ticker              TEXT        NOT NULL DEFAULT 'PORTFOLIO',
    description         TEXT,

    UNIQUE (watchdog_run_id, ticker, issue_type)
);

CREATE INDEX IF NOT EXISTS idx_watchdog_logs_date           ON watchdog_logs (watchdog_date DESC);
CREATE INDEX IF NOT EXISTS idx_watchdog_logs_status         ON watchdog_logs (overall_status);
CREATE INDEX IF NOT EXISTS idx_watchdog_logs_health         ON watchdog_logs (pipeline_health_score DESC);
CREATE INDEX IF NOT EXISTS idx_watchdog_issues_run          ON watchdog_issues (watchdog_run_id);
CREATE INDEX IF NOT EXISTS idx_watchdog_issues_severity     ON watchdog_issues (severity);
CREATE INDEX IF NOT EXISTS idx_watchdog_issues_ticker       ON watchdog_issues (ticker);
