-- ============================================================
-- Orblex Industries — Smart Money Schema
-- Run this in your Supabase SQL Editor before the first
-- run_smart_money.py run. Idempotent: safe to re-run.
--
-- Source: SEC EDGAR, free and keyless.
--   13F-HR       — each fund's full long book, quarterly, filed up to
--                  45 days after quarter end. Drives investor_holdings.
--   Form 4       — >10% owners' trades, filed within 2 business days.
--   Schedule 13D/G — >5% stakes (13D = activist intent), 5–10 business days.
--                  Form 4 + 13D/G drive investor_filings.
-- ============================================================


-- ── TRACKED INVESTORS — one row per fund, latest 13F summary ─────────────────

CREATE TABLE IF NOT EXISTS smart_money_investors (
    cik               BIGINT      PRIMARY KEY,
    manager           TEXT        NOT NULL,     -- the famous person, e.g. "Warren Buffett"
    fund              TEXT        NOT NULL,     -- filer, e.g. "Berkshire Hathaway"
    style             TEXT,                     -- Value / Activist / Macro / Growth
    period_of_report  DATE,                     -- quarter end of the latest 13F
    prev_period       DATE,                     -- quarter the changes are measured against
    filing_date       DATE,
    accession         TEXT,                     -- latest 13F accession used (skip-if-unchanged key)
    total_value_usd   NUMERIC(20, 2),
    positions         INT,
    new_count         INT,
    added_count       INT,
    reduced_count     INT,
    sold_count        INT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── HOLDINGS — latest 13F per investor, diffed against the prior quarter ─────
-- Replaced wholesale for an investor when a newer 13F lands, so this table is
-- always "what each fund holds now" (plus SOLD rows for positions it exited).

CREATE TABLE IF NOT EXISTS investor_holdings (
    id                BIGSERIAL   PRIMARY KEY,
    run_id            TEXT,
    cik               BIGINT      NOT NULL,
    manager           TEXT        NOT NULL,
    fund              TEXT        NOT NULL,
    period_of_report  DATE        NOT NULL,
    filing_date       DATE,
    accession         TEXT,
    cusip             TEXT        NOT NULL,
    ticker            TEXT,                     -- NULL when OpenFIGI has no US listing
    issuer            TEXT,
    title_class       TEXT,
    put_call          TEXT        NOT NULL DEFAULT '',   -- '' = shares, else PUT / CALL
    shares            NUMERIC(20, 2),
    prev_shares       NUMERIC(20, 2),
    value_usd         NUMERIC(20, 2),
    pct_portfolio     NUMERIC(8, 4),
    share_change_pct  NUMERIC(12, 2),
    change_type       TEXT,                     -- NEW | ADDED | REDUCED | UNCHANGED | SOLD | NULL (no prior quarter)
    rank              INT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (cik, cusip, put_call)
);

CREATE INDEX IF NOT EXISTS idx_investor_holdings_ticker ON investor_holdings (ticker);
CREATE INDEX IF NOT EXISTS idx_investor_holdings_cik    ON investor_holdings (cik);


-- ── RECENT FILINGS — Form 4 / Schedule 13D / 13G by tracked investors ────────

CREATE TABLE IF NOT EXISTS investor_filings (
    id                BIGSERIAL   PRIMARY KEY,
    run_id            TEXT,
    cik               BIGINT      NOT NULL,
    manager           TEXT        NOT NULL,
    fund              TEXT        NOT NULL,
    form              TEXT        NOT NULL,
    filing_date       DATE        NOT NULL,
    accession         TEXT        NOT NULL,
    ticker            TEXT,
    issuer            TEXT,
    direction         TEXT,                     -- BUY | SELL | OTHER (Form 4) · STAKE (13D/G)
    shares_bought     NUMERIC(20, 2),
    shares_sold       NUMERIC(20, 2),
    avg_price         NUMERIC(14, 4),
    shares_after      NUMERIC(20, 2),
    pct_class         NUMERIC(8, 3),            -- 13D/G percent of class, when disclosed
    summary           TEXT,
    url               TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (cik, accession)
);

CREATE INDEX IF NOT EXISTS idx_investor_filings_ticker ON investor_filings (ticker);
CREATE INDEX IF NOT EXISTS idx_investor_filings_date   ON investor_filings (filing_date DESC);


-- ── CUSIP → TICKER CACHE (OpenFIGI) ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS cusip_ticker_map (
    cusip       TEXT        PRIMARY KEY,
    ticker      TEXT        NOT NULL,
    name        TEXT,
    source      TEXT        NOT NULL DEFAULT 'openfigi',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── RLS — the dashboard reads with the anon key ──────────────────────────────
-- Public SEC data, so read-only access for anon/authenticated. Writes come
-- from the pipeline's service-role key, which bypasses RLS.

ALTER TABLE smart_money_investors ENABLE ROW LEVEL SECURITY;
ALTER TABLE investor_holdings     ENABLE ROW LEVEL SECURITY;
ALTER TABLE investor_filings      ENABLE ROW LEVEL SECURITY;
ALTER TABLE cusip_ticker_map      ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "read smart_money_investors" ON smart_money_investors;
DROP POLICY IF EXISTS "read investor_holdings"     ON investor_holdings;
DROP POLICY IF EXISTS "read investor_filings"      ON investor_filings;

CREATE POLICY "read smart_money_investors" ON smart_money_investors FOR SELECT TO anon, authenticated USING (true);
CREATE POLICY "read investor_holdings"     ON investor_holdings     FOR SELECT TO anon, authenticated USING (true);
CREATE POLICY "read investor_filings"      ON investor_filings      FOR SELECT TO anon, authenticated USING (true);
-- cusip_ticker_map is pipeline-internal: RLS on, no policy → anon cannot read it.
