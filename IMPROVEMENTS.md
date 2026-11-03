# Orblex Industries — System Review & Improvement Roadmap

Findings from a full read of the pipeline (orchestrator, six agents, policy layer,
schemas, backtest engine, dashboard) as of **2026-09-08**, against pipeline output
`newsletter_2026-09-08.html`.

Items are ordered by dependency, not by size: the correctness fixes in Part 1 must
land before the feature work in Parts 2–4 is worth building on.

---

## Part 0 — How the system works today

Daily flow (`run_all.py`, GitHub Actions 13:30 UTC Mon–Fri, NYSE holiday-gated):

| Stage | Script | Purpose | LLM calls |
|---|---|---|---|
| 0 | `run_macro` / `run_earnings` / `run_regime` / `run_fundamentals` | BLS/FRED macro, earnings calendar, SPY/QQQ/VIX regime, valuation snapshots (parallel) | 0 — regime is rule-based by design |
| 1+2 | `pipeline.py` / `run_options.py` | OHLCV ingest → `stock_prices`; options chain, P/C ratio, max pain | 0 |
| 3 | `run_agent.py` | Per-ticker BUY/SELL/HOLD + sentiment + valuation view | 31 (one per ticker) |
| 3.5 | `run_drift.py` | Signal-flip detection → `signal_history`, `signal_drift` | 0 |
| 4 | `run_risk.py` | `risk_score` 1–10, `max_position_pct` per ticker | 1 |
| 5 | `run_allocator.py` | Deterministic base weights, then model refinement | 1 |
| 6 | `run_report_writer.py` | Narrative synthesis | 1 |
| 7 | `run_watchdog.py` | Cross-agent consistency audit | 1 |
| 8 | `run_newsletter.py` | HTML + plaintext email | 1 |

The core architectural principle is sound and worth preserving explicitly:
**hard constraints live in Python, judgment lives in the model.**
`_compute_base_weights()` computes the allocation deterministically; the model only
refines within those bounds, with a fallback to base weights when its output does
not sum to 100. Regime detection deliberately avoids an LLM.

---

## Part 1 — Correctness fixes (blocking)

### 1.1 `instrument_policy.py` is dead code in the live path — **CRITICAL**

`run_all.py` Stage 5 invokes `run_allocator.py`, which is a *fork* of
`allocator_agent.py` and never imports `instrument_policy`. None of the following
executes in production:

- regime eligibility gating (`ELIGIBLE_REGIMES`)
- inverse sleeve caps (`INVERSE_MAX_PCT` 5%, `INVERSE_MAX_HOLD_DAYS` 10)
- defensive sleeve gating and `DEFENSIVE_MAX_PCT`
- sector caps (`SEMIS` 30%, `SOFTWARE` 30%) and theme caps (`TECH` 80%)
- the post-LLM clamp `enforce_allocation_constraints()`

**Observed impact**, `newsletter_2026-09-08.html`, regime `BULL`:

```
SH   2.3%   ← -1x S&P 500 daily-reset ETF; policy says BEAR-only
XLP  3.4%   ← policy says CORRECTION/BEAR only
XLU  2.2%   ← policy says CORRECTION/BEAR only
GLD  3.0%   ← policy says CORRECTION/BEAR only
            = 10.9% of book in policy-forbidden instruments
```

Holding an inverse index ETF through a confirmed bull market is a structural drag
and is exactly the failure the policy layer was written to prevent.

**Fix:** consolidate to a single allocator that imports `instrument_policy`, wires
the regime through, and calls `enforce_allocation_constraints()` after the model.

### 1.2 Market regime never reaches allocation — **CRITICAL**

`allocation_context_builder.py` fetches `regime` and `regime_risk_floor` and places
them in the context. `run_allocator.py` contains zero references to either. The
regime's `risk_floor` — the mechanism that escalates the cash floor to 20% in a
CORRECTION and 35% in a BEAR — is inert. The regime is display-only in the
newsletter.

**Fix:** apply `_effective_risk_level(market_risk_level, regime_risk_floor)`, which
escalates risk only (`max()`, never `min()`), before computing the cash floor.

### 1.3 The newsletter prints an unconditional false all-clear — **HIGH**

`allocation_saver.py` reads `allocation.get("sector_exposure", {})` and the
newsletter reads `constraint_violations` — keys `run_allocator.py` never sets, and
`constraint_violations` is not even a column on `allocation_plans`. The banner

> CONSTRAINT ENFORCEMENT (post-LLM, Python-guaranteed)
> Model allocation was fully compliant — no corrections needed.

therefore prints every single day regardless of what happened. A check that always
passes is worse than no check, because it stops anyone from looking.

**Fix:** add the `constraint_violations` column, persist real enforcer output, and
have the newsletter distinguish "no violations" from "enforcement did not run."

**Follow-up required — the column was added with a default.** All 43 pre-existing
`allocation_plans` rows read back as `[]` rather than `NULL`, so every historical
plan now claims the enforcer ran and found nothing. That is the same false
all-clear this item set out to remove, just relocated into the backfill. The
column must default to `NULL` and the pre-fix rows must be reset:

```sql
ALTER TABLE allocation_plans ALTER COLUMN constraint_violations DROP DEFAULT;

-- No plan written before the enforcer landed was actually verified.
UPDATE allocation_plans
   SET constraint_violations = NULL
 WHERE plan_date < '2026-09-08';
```

No information is lost: the `[]` values were produced by the DDL, not by an
enforcer run.

### 1.4 Two divergent allocator implementations — **MEDIUM**

`allocator_agent.py` has the policy integration but `max_tokens=2048` and no
truncation detection. `run_allocator.py` has `max_tokens=8192` and truncation
detection but no policy. Neither has both, and the more correct one is the dead
one. Nothing in the repo imports `allocator_agent.py`.

**Fix:** merge into `run_allocator.py`, delete `allocator_agent.py`.

### 1.5 Risk agent output truncation risk — **MEDIUM**

`risk_agent.py` uses `max_tokens=4096` while asking for `ticker_details` covering
all 31 tickers. When the model drops tickers, the placeholder path inserts
`risk_score: 5, max_position_pct: 15.0` — a *moderate* default, meaning a dropped
ticker silently becomes an ordinary sized position rather than being excluded.

**Fix (done):** `MAX_TOKENS` 4096 → 8192, `stop_reason == "max_tokens"` checked and
logged, and the placeholder now **fails closed** — `risk_score=8`,
`max_position_pct=0.0`, `risk_flags=["ASSESSMENT_MISSING"]`, which puts the ticker
past the allocator's exclusion threshold. Missing tickers are added to
`flagged_tickers` and the result carries `coverage_complete` / `missing_tickers` /
`response_truncated`, so a degraded run is visible downstream rather than only in
a log tail.

Verified by simulating a truncated response over 8 tickers where the model
returned only 5: the 3 dropped names were pinned, excluded at 0.0% by the
allocator, and flagged, with the allocation still summing to 100.0%. Under the
old placeholder those 3 would each have been sized as ordinary ~15% positions in
names nobody assessed.

### 1.6 Log handlers crash on model output — **LOW, cross-cutting**

Model text routinely contains `≤`, `—`, `≥`. Every runner builds its handlers as
`logging.FileHandler("x.log")` with no encoding, so on Windows the cp1252 default
raises `UnicodeEncodeError` *inside the handler*, burying real output under
tracebacks. Fixed in `run_allocator.py` (UTF-8 file handler + `sys.stdout`
reconfigure); the same two-line change is still needed in `run_agent.py`,
`run_risk.py`, `run_report_writer.py`, `run_watchdog.py`, `run_newsletter.py`,
`run_regime.py`, `run_drift.py`, and `run_options.py`.

---

## Part 2 — Close the feedback loop

### 2.1 Backtest and drift results are write-only

`backtest_results` and `signal_drift` are computed and stored, and no agent prompt
ever reads them. The system measures whether it was right and then discards the
answer. It cannot notice that, for example, its STRONG BUYs on >50%-volatility
names have been coin flips.

- [x] Run the backtest nightly as a Stage 9 rather than manually via
  `run_backtest.py`. Wired into `run_all.py`: added to the default stage set,
  runs last (reads only `allocation_plans` + `stock_prices`, so it is
  run-independent and cannot block the newsletter), honours `--dry-run`. The
  scheduled GitHub Actions job passes no `--stages`, so it picks up Stage 9
  automatically.
- [x] Feed a compact performance block into the analyst and risk prompts:
  per-ticker hit rate at 5d/20d, portfolio alpha vs SPY, current drawdown. New
  `performance_context.py` reads the latest `backtest_runs`/`backtest_results`/
  `backtest_ticker_results` back into a compact summary and formats two blocks —
  the analyst gets its own ticker's hit rate plus portfolio alpha/drawdown; the
  risk manager gets portfolio alpha/drawdown plus the lowest-hit-rate names as
  tighter-cap candidates. Wired into `run_agent.py` (once per run) and
  `run_risk.py`, with system-prompt rules telling both to treat feedback as a
  calibration/capital-protection input, never as a standalone signal, and to
  ignore `N/A` (no matured history) rather than read it as zero. Drawdown is a
  proxy: the paper book's equity curve chained from realised 1-day returns
  (no live `positions` table until Part 4).
- [x] Apply a **mechanical** strength haircut in Python to chronic flippers
  (`signal_drift.is_chronic`) rather than asking the model to be consistent.
  `allocation_context_builder.py` reads the chronic set (an `is_chronic` drift
  row within the last 7 days — `run_drift` stays the single source of truth) and
  tags each `ticker_input`. `_compute_base_weights()` in `run_allocator.py`
  drops a chronic name's sizing strength one notch (STRONG→MODERATE→WEAK; WEAK is
  already floored) for the multiplier only — the analyst's stored
  `signal_strength` is untouched — so the deterministic base weight shrinks and
  the freed weight redistributes to stable names. The reduction is logged and
  surfaced to the model as `CHRONIC_HAIRCUT=<a>-><b>` with a "do not restore"
  rule, keeping the constraint in Python per the architecture principle.

### 2.2 The analyst has no memory of its own prior call

`context_builder.py` builds each day's context from scratch: prices, fundamentals,
news. It never sees what it decided yesterday. This is a direct cause of the
flip-flopping that `run_drift.py` faithfully records every day.

**Fix:** include prior signal, prior strength, and days-held in the analyst prompt.

**Done.** `context_builder._query_prior_signal()` reads the append-only
`signal_history` log (written by `run_drift` *after* each analyst run, so at
analyst time its latest rows are genuinely the prior calls, never today's) and
returns the last signal/strength, the date it was issued, `held_sessions`
(consecutive prior runs on the same signal), and `days_held` (calendar days since
it last changed). The analyst prompt gains a PRIOR DECISION block and a
system-prompt rule to prize consistency — reverse a recent call only on a named
material change, not on noise — which targets the whipsaw `run_drift` records.
Degrades to "No prior decision on record" for a ticker's first-ever run.

### 2.3 Stage 0 data is orphaned

Options flow, earnings calendar, macro indicators, and regime reach only the
**newsletter** — not the analyst, not risk, not allocation. The pipeline pays to
fetch them and never decides on them.

**Fix:** add to the analyst context: days-to-earnings, put/call ratio, IV rank, the
regime line, one macro sentence. Earnings proximity in particular should be a
sizing input, not a footnote — a name reporting in 3 days carries different risk
than one reporting in 70.

**Done (analyst).** `context_builder` now pulls per-ticker `earnings_calendar`
(days-to-earnings, event risk, last surprise, beat streak) and the nearest-expiry
`options_flow` row (put/call vol+OI, IV rank, max pain, flow signal); a new
`build_market_context()` fetches the `market_regimes` line and `macro_snapshots`
sentence ONCE per run (identical across tickers) and `run_agent` injects it into
each context. The analyst prompt gains MARKET CONTEXT / EARNINGS / OPTIONS FLOW
sections with rules: earnings within ~5 trading days caps `signal_strength` and
flags binary event risk; options flow and IV rank corroborate or soften the
technical read without inverting it; a risk-floor regime or hostile macro biases
toward conservatism. Every field degrades to N/A.

**Extended to risk + allocator.** `risk_context_builder` now enriches each
ticker with days-to-earnings, earnings risk, put/call, IV rank, and flow signal
(reusing `context_builder`'s fetchers) and attaches the regime+macro
`market_context`; the risk prompt gains a MARKET CONTEXT block and rules —
imminent earnings raise risk_score / tighten caps, high IV rank or conflicting
flow raise risk, a risk-floor regime lifts market_risk_level portfolio-wide.
`allocation_context_builder` adds `days_to_earnings` per ticker; the allocator
prompt marks names reporting within 5 days `EARNINGS_IN=<n>d` with a rule to keep
them at/below base weight (hard sizing still owned by the Risk Manager's cap).

**iv_rank producer fix (folded in).** `options_flow.iv_rank` was never populated
(column existed, saver omitted it). `run_options` now computes it: current ATM IV
and its trailing history are both sampled from `options_strikes` (top strikes by
OI, near-the-money by |delta| 0.35–0.65 with an all-IV fallback), and iv_rank is
the current level's position in that history's [min,max]. It stays NULL when
history is under 8 days or flat, or in dry-run — never fabricated. Existing rows
read N/A until the next live options run backfills the column.

### 2.4 Backtest metric quality

- `HOLD` correctness is defined as `abs(return) < 3%`, which is arbitrary and
  rewards HOLD on low-volatility names regardless of skill.
- No risk-adjusted metrics (Sharpe, max drawdown, hit rate by signal strength).
- No transaction costs or slippage, which makes any measured alpha optimistic.
**Done.** Addressed without schema changes (the backtest tables have no
migration and adding columns would break the upserts), so new metrics live in the
returned summary + printed report:
- HOLD correctness is now volatility-relative: a HOLD scores correct only if the
  realised move stayed within the name's own trailing 1-sigma expected move over
  the horizon (`_trailing_daily_vol` + `_hold_is_correct`), falling back to the
  old 3% band only when vol is uncomputable. This flows through `signal_correct`,
  so the analyst/risk hit-rate feedback (2.1) improves automatically.
- Risk-adjusted + cost metrics added to the summary: a per-plan Sharpe-like
  ratio (return/vol, not annualised), max drawdown of the 1d-chained paper book,
  hit rate by signal strength (STRONG vs MODERATE vs WEAK), and a net-of-cost
  return + net alpha using a documented 5 bps/side round-trip assumption. First
  run already shows STRONG 80% vs MODERATE 59.5% — a spread the old aggregate hid.
- SPY coverage fixed: `_ensure_spy_prices` now compares SPY dates against the
  portfolio's trading calendar and backfills the gap (previously it skipped as
  soon as a single SPY row existed, leaving 20d alpha permanently N/A). Honours
  `--dry-run`.

- **SPY benchmark coverage is sparse** (surfaced while wiring Stage 9). The
  summary now renders unmatured windows as `N/A` instead of `nan` and prints a
  coverage column (`spy_periods`/`periods`): as of 2026-09-10 SPY has a matured
  benchmark for only 19/44 plans at 1d, 14/44 at 5d, and 0/44 at 20d, while
  portfolio/eq-weight returns are complete. `_ensure_spy_prices()` fetches SPY
  only when the range is entirely empty, so gaps inside the range are never
  backfilled — that is the likely cause and the fix. Piece 2 below must treat a
  `None`/absent alpha as "no benchmark yet," never as zero.

---

## Part 3 — User experience

The newsletter is currently doing all the work; the dashboard is a local-only Vite
app. In rough value order:

1. **"What changed" section** — signal flips, allocation deltas ≥ 1pp, new
   anomalies, names entered/exited. Today the newsletter is a full state dump and
   the reader has to diff it mentally against yesterday.

   **Done.** `collect_change_data()` in `run_newsletter.py` computes the diff
   deterministically in Python (per the architecture principle — no second model
   narrative): flips read straight from `signal_drift` scoped to *today's*
   `report_date` (so a quiet day shows "no flips" rather than resurfacing a stale
   one, and `run_drift` stays the single source of truth), and the allocation diff
   compares the current `allocation_plans` run against the immediately prior one —
   names crossing the 0.5% book threshold render as ENTER/EXIT, held-in-both names
   moving ≥ 1pp render as WEIGHT, each sorted by magnitude. A new WHAT CHANGED
   section sits directly under Portfolio Stance in both the HTML and the plaintext
   part. Three states: the diff (badged FLIP/ENTER/EXIT/WEIGHT rows), a "book is
   steady" note when a prior exists but nothing moved, and the section omitted
   entirely on a first-ever run with no prior plan. Verified over synthetic
   fixtures covering all three. **Not included:** watchdog "anomalies" — the
   newsletter surfaces no watchdog data today, and a diff-of-diffs against prior
   `watchdog_issues` would add noise; deferred as its own item if wanted.
2. **Per-ticker decision trace** — one expandable view showing
   `raw indicators → analyst call → risk cap → base weight → model adjustment →
   enforcer clamp`. Every piece is already in Supabase; nothing surfaces the chain.

   **Done.** A `DecisionTrace` component in the dashboard's ANALYST view renders
   the full six-step chain for the selected ticker, each value read straight from
   state (an audit trail, not a second opinion):
   1. **Raw indicators** — recomputed client-side from `stock_prices` using
      `context_builder.py`'s exact formulas *and* its 30-day lookback, so RSI/MA5/
      MA20/vol/close reproduce the analyst's own numbers (verified: AAOI RSI 36.1,
      MA20 $120.05, vol 112.0%, close $111.55 all match the stored reasoning).
      `signal_history` was the obvious source but its indicator columns are never
      populated — the DRIFT view's RSI/VOL columns are blank for the same reason.
   2. **Analyst call** — signal/strength/sentiment/valuation (`agent_reports`).
   3. **Risk cap** — score, max-position %, flags (`risk_ticker_details`).
   4. **Base weight** — the deterministic pre-model weight. This was *computed but
      never persisted*; added a `base_weight` column to `allocation_details` (NULL
      default — pre-fix rows read "unknown", never a fabricated number) and wired
      `allocation_saver` to write it from the allocator's `base_weights`.
   5. **Final weight** — `allocated_pct` with the Δ vs base, sizing method, and
      rationale (net model+enforcer move off base; the model's raw pre-enforcer
      output is not persisted, so this is framed as net rather than a fake split).
   6. **Enforcer clamp** — the `constraint_violations` entries naming this ticker,
      or a green "no correction," or an amber "not verified" when the enforcer
      never ran.

   **Bonus fix (found while wiring step 1):** the dashboard's price fetch used
   `.order(asc).limit(2000)`, but PostgREST caps a response at 1000 rows, so it
   silently returned the *oldest* 1000 and dropped every August/September session —
   the chart, price table, day-change metric, and (would-be) trace indicators were
   all stuck weeks in the past. Replaced with newest-first pagination that sorts
   each series ascending; day-change for AAOI corrected from a stale +17.76% to the
   real +5.70%.

   **Requires one manual step to fully light up step 4:** apply the `base_weight`
   migration in `agent_schema_v2.sql` to Supabase, then the next allocator run
   populates it. Until then step 4 shows "unknown — plan predates the column."

   **Resilience follow-up (2026-09-14).** Until the migration was applied the
   missing `base_weight` column did not just blank step 4 — it made PostgREST
   reject the *entire* `allocation_details` write (`PGRST204`), so the allocator
   hard-failed and cascaded into the report writer, watchdog, newsletter and
   backtest (an entire daily run lost). `allocation_saver.py` now catches that one
   specific error, drops the un-migrated column, and retries the write, logging a
   warning that points at the migration. The rest of the row is preserved and the
   pipeline completes; step 4 still reads "unknown" until the column exists.
3. **Scorecard band** — hit rate, alpha vs SPY, current drawdown, at the top of the
   newsletter. It is the first thing a reader wants and it is absent.

   **Done.** A SCORECARD band renders directly under Portfolio Stance in both the
   HTML and plaintext parts, showing the three numbers a reader asks for first:
   signal hit rate, alpha vs SPY, and current paper-book drawdown. It reads the
   *same* `performance_context.build_performance_summary()` the analyst and risk
   prompts consume (item 2.1), so the band the reader sees is exactly the feedback
   the system fed itself — no second computation to drift out of sync.
   `performance_context` gained a portfolio-wide `hit_rate_overall` (pooled across
   every scored ticker outcome, weighted by call count, computed from raw rows so
   it is exact rather than an average of rounded per-ticker rates). Each metric
   prefers the 20d window and falls back to 5d; colour tone is green/amber/red on
   value (hit rate ≥55/≥45, alpha sign, drawdown ≥−0.05/≤−10). Per the 2.4/2.1
   rule, an unmatured window renders as **N/A with the reason** ("no window matured
   (N pending)", "insufficient history") — never as a zero that would read as
   "flat" — and the whole band is omitted on a system with no backtest yet.
   Verified over synthetic fixtures (full data, all-N/A, 5d-only fallback, empty).
   First live render: hit rate 65% (46 calls), alpha −0.10% (24 matured 20d plans),
   drawdown −1.5% (worst −2.6% / 45d).
4. **Deploy the dashboard** — Vercel + Supabase RLS with the anon key, so it is
   reachable from a phone rather than `npm start` on one laptop.

   **Done.** The dashboard is deployed on Vercel (`vercel.json`: Vite build to
   `dist/`, SPA rewrite for every non-`/api` route). The frontend reads through
   the anon key under the existing RLS policies. Merging into `release` deploys to
   production; other branches get preview deploys. Vite splits the vendor code into
   `react` / `charts` / `supabase` chunks so browsers keep them cached across
   deploys. Deployment added a **RUN PIPELINE** button: the user signs in with a
   Supabase magic link, and `api/run-pipeline.js` re-verifies the session
   server-side, checks the email against `PIPELINE_ADMIN_EMAILS`, returns 409 if a
   run is already in progress, and dispatches `orblex_pipeline.yml` on `release`.
   The GitHub token never reaches the browser. The workflow was made manual-only
   at the same time, so every run is deliberate.
5. **Intraday alerts** — a lightweight watcher for hard-threshold breaches (a held
   name gapping >5%, an earnings surprise, a VIX regime flip) pushed to
   Telegram/Discord. The current design cannot say anything between 8:30am prints.

---

## Part 4 — Personal trading use

The system produces **target weights**. Trading requires **deltas against what is
actually owned**. No schema in the repo has a holdings, cost-basis, or cash table;
allocations are abstract percentages recomputed from zero each morning with no
turnover awareness. This is the single largest gap.

1. **`positions` table** — ticker, shares, cost basis, entry date, plus account
   cash. Manual entry first; broker sync later.
2. **Rebalance / orders stage** — diff target weights against actual positions,
   apply a turnover threshold (do not trade a 0.4% drift), respect lot sizes and
   account size, and emit a concrete order list
   (`SELL 12 SH @ market`, `BUY 4 AVGO`). This is the step that turns a research
   report into something usable.
3. **Real P&L tracking** — realized and unrealized per position, versus SPY, versus
   the model's own paper portfolio. The system currently cannot answer "is this
   making money."
4. **Tax awareness** — flag wash-sale windows and short-term/long-term holding
   boundaries before emitting a SELL. Cheap to add, expensive to omit.
5. **Paper-trading mode first** — write to a simulated `positions` table for 60–90
   days before any real capital moves. That produces the track record that
   1/5/20-day backtest windows cannot.
6. **Broker integration last** (Alpaca has a clean paper API), kept behind explicit
   human confirmation. The fact that the constraint enforcer was silently bypassed
   for weeks is itself the argument against auto-execution.

---

## Explicit non-goal

**Do not add more agents.** Six is already past the point of marginal return; each
additional one adds latency and cost without adding information. Every item above
is about wiring what already exists — the orphaned Stage 0 data, the dead policy
layer, the write-only feedback tables — rather than new capability.

---

## Status

- [x] **1.1** `instrument_policy` wired into the live allocator
- [x] **1.2** Regime risk floor applied to allocation
- [x] **1.3** `constraint_violations` persisted; newsletter all-clear made honest
- [x] **1.4** Allocator implementations consolidated
- [x] **1.5** Risk agent truncation hardening — fails closed on missing coverage
- [x] **1.6** Log encoding — UTF-8 handlers across all 9 runners
- [x] **Part 2** Feedback loop — **complete**. 2.1: Stage 9 nightly backtest;
  performance block fed into analyst + risk prompts; mechanical chronic-flipper
  haircut in the allocator. 2.2: analyst now sees its own prior
  signal/strength/days-held. 2.3: Stage 0 data (regime, macro, earnings, options
  flow) wired into the analyst — then extended to the risk and allocator prompts;
  iv_rank producer bug fixed. 2.4: vol-relative HOLD correctness, risk-adjusted +
  cost metrics, SPY coverage backfill.
- [ ] **Part 3** UX — 3.1 "What changed" section done (deterministic diff:
  flips + entered/exited + weight moves ≥1pp, HTML + plaintext). 3.2 per-ticker
  decision trace done (6-step chain in the ANALYST view; base_weight now
  persisted; fixed the 1000-row price-fetch truncation as a side effect). 3.3
  scorecard band done (hit rate / alpha vs SPY / paper drawdown at the top of the
  newsletter, from the same performance_context the prompts use; N/A when
  unmatured). 3.4 dashboard deployed on Vercel with an authenticated RUN
  PIPELINE button. 3.5 pending.
- [ ] **Part 4** Personal trading

### Verification of the Part 1 fixes

Dry run against live Supabase data, 2026-09-08 signals, regime `BULL`:

```
Regime BULL: 4 ticker(s) policy-excluded — GLD, SH, XLP, XLU (REGIME_EXCLUDED)
Effective risk : HIGH  (market=HIGH, regime=BULL)
Invested       : 80.0%   Cash: 20.0%
Sector exposure: SEMIS 28.29%  SOFTWARE 17.58%  ...   (both under 30% cap)
Constraint enforcer: model output was already compliant.
```

The four policy-forbidden instruments now arrive at the model already zeroed and
are re-checked afterwards. Feeding the enforcer the *old* buggy allocation
reproduces the catch:

```
- GLD: 3.0% but REGIME_EXCLUDED in BULL — forced to 0%
- SH:  2.3% but REGIME_EXCLUDED in BULL — forced to 0%
- XLP: 3.4% but REGIME_EXCLUDED in BULL — forced to 0%
- XLU: 2.2% but REGIME_EXCLUDED in BULL — forced to 0%
- Sector SEMIS: 40.8% > cap 30.0% — scaled by 0.735
```

Note the fifth line: once the defensive sleeve is correctly excluded, its weight
redistributes into semis and pushes SEMIS to 40.8%. The sector cap that was also
dormant is what catches it. The two bugs were masking each other.

**Known behaviour, not a bug:** sector-cap scaling removes weight and the enforcer
lets cash absorb it rather than redeploying into other sectors, so a heavily
capped day ends with cash above its floor (69.19% invested / 30.81% cash in the
reproduction above). This is deliberate — redeployment would need a second
optimisation pass — but it is worth revisiting if cash drag shows up in the
backtest.
