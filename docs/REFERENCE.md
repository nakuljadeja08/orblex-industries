# Orblex Industries — Agentic Portfolio Management

Every weekday, a Python pipeline turns market data into swing trade plans and saves them to Postgres. Rule code sizes and checks each plan, you approve it on a dashboard, and you place it in Robinhood by hand. On Saturdays, the same pipeline writes rebalance advice for your main account, which it never trades. Jobs run on GitHub Actions, dispatched on time by Supabase `pg_cron`; nothing runs on your PC or inside a Claude session.

> **Disclaimer:** It is built to trade a small real account (a Robinhood agentic account) under per-order human approval. Nothing here constitutes financial advice. Claude never places orders. An executor that can send orders to Robinhood's approval queue is built and tested, and stays off unless you set the repository variable `EXECUTOR_LIVE=1`.

---

## Contents

- [Two books](#two-books)
- [A trading day](#a-trading-day)
- [Limits, day trades and the executor](#limits-day-trades-and-the-executor)
- [Design principles](#design-principles)
- [System architecture](#system-architecture)
- [Pipeline stages](#pipeline-stages)
- [The agents](#the-agents)
- [Constraints and the policy layer](#constraints-and-the-policy-layer)
- [Feedback loop](#feedback-loop)
- [Dashboard](#dashboard)
- [Newsletter](#newsletter)
- [Project structure](#project-structure)
- [Setup](#setup)
- [Running locally](#running-locally)
- [Deployment and CI](#deployment-and-ci)
- [Database](#database)
- [Project history](#project-history)
- [Roadmap](#roadmap)

---

## Two books

| | Swing book | Long-term book |
|---|---|---|
| Account | Robinhood agentic account (`RH_AGENTIC_ACCOUNT`) | Main account, read for % weights only |
| Runs | NYSE sessions, 6:30 ET | Saturday, 9:00 ET (holdings snapshot at 8:30) |
| Settings | `TRADING_MODE=swing`, `BOOK=daily` | `TRADING_MODE=position`, `BOOK=longterm` |
| Run id | `pipeline-YYYY-MM-DD-xxxxxx` | `longterm-YYYY-MM-DD-xxxxxx` |
| Horizon | 1–10 sessions, long only | Weeks to months |
| Output | Limit, stop, target, horizon → `trade_orders` | NEW / ADD / TRIM / EXIT / HOLD / AVOID → `longterm_advice` |
| Can it trade? | Only through your approval | Never |

The allocator, report writer and watchdog run only in the Saturday book. They feed no swing order, so weekday runs skip them (`run_all.py`, `WEEKLY_STAGES`). The ALLOCATION, REPORT and WATCHDOG tabs show Saturday's run. An explicit `--stages` list still runs them.

---

## A trading day

Times are ET and come from `automation_schedule`. Each job is dispatched at most once per day, can start up to 45 minutes late after an outage, and records a `job_runs` row. Weekday jobs record `SKIPPED` when NYSE is closed.

| ET | Job | What it does |
|---|---|---|
| 6:30 | `pipeline` | `run_all.py`: swing plans, risk, newsletter (GitHub cron at 7:45 is the fallback) |
| 8:40 | `morning_plan` | Waits up to 35 min for the risk stage, reads the account, syncs fills, builds `trade_orders`, emails what to approve, and reminds you when no mandate is set |
| any time | you | Approve on TRADE PLANS, then place each order in Robinhood exactly as its ticket says |
| 9:25 | `dryrun` | The real executor against live reads through `DryRunBroker`, or the live executor if the switch is on |
| 10:20, 13:20, 16:20 | `fill_sync` | Links your orders, records fills, emails `STOP MISSING` once a day per position with no covering stop, including today's fills, with the plan stop |
| 10:30, 13:30, 15:30 | `executor_pass` | Live executor only (rows ship disabled): approvals after 9:25 and stops on today's fills |
| 16:35 | `eod_review` | Compares the dry run with what you did → `daily_reviews`, email |
| Sat 8:30 | `holdings_snapshot` | Main-account % weights → `longterm_holdings` |
| Sat 9:00 | `longterm_run` | The long-term book (GitHub cron at 15:00 UTC is the fallback, after this in both seasons) |

---

## Limits, day trades and the executor

Three layers, each only able to tighten the one above: constants in `exec_limits.py`, your daily mandate on TRADE PLANS, and per-order approval.

| Ceiling | Now |
|---|---|
| Risk per trade, before strength × level | 2.5% of equity |
| Notional per position | 30% |
| Open risk across positions | 5% |
| Open positions, including working entries | 5 |
| New entries per day / per 5 sessions | 2 / 6 |
| Orders per day, resubmits per order, cancel or replace per order per day | 8, 1, 2 |
| Consecutive stop-outs before entries stop | 2 |
| Daily loss / drawdown halt | 3% / 10% |
| Day trades per 5 sessions under $25k | 3, the 4th is refused |

**How the risk numbers fit:** open risk is the limit that binds. Two full-size STRONG trades use all 5%, so five positions only fit at about 1% each. On a small account that is normal, because WEAK plans and whole shares often size entries well under 1%. So the 5-position limit only counts names, and 2.5% is the most one trade may take. See `EXECUTOR_DESIGN.md` §1.3 for the replay behind this.

**Day trades (your decision, 2026-11-02):** same-day exits are allowed and same-day re-buys are not. A protective stop can rest from the fill, and it counts as a day trade only if it fires. `trade_guard.py` refuses a sell of a ticker bought today when it could be the 4th day trade in 5 sessions, or when the count can't be read (`PDT_LIMIT`), and stops already resting on other fills from today count toward that. Setting `ALLOW_SAME_DAY_EXIT = False` restores the old rule. `swing_backtest.py` still scores trades without fill-day exits so its history stays comparable; `--limits` on `swing_screen_backtest.py` shows the difference.

**Executor** (`executor.py`, `run_dryrun.py`, `run_executor.py`, `rh_trader.py`): one pass syncs fills, reconciles ledger and broker (any mismatch halts it), sends exits and protective OCOs, then APPROVED entries through `check_submission()`. It replaces only your typing of the order: each order still waits for your tap in Robinhood. `rh_trader.py` refuses to send unless `EXECUTOR_LIVE=1`, the account is the agentic account (`RH_AGENTIC_ACCOUNT`), and `human_must_approve_trades` reads true in the same session. Retries can't double an order, because the id is sent as a UUIDv5 `ref_id`.

**Turning it on, when the dry-run gate passes:**
1. Set a real mandate every day of the dry run. Most clean days so far used an assumed one (`mandate_source` in `exec_dryrun`).
2. Re-run `automation_schema.sql`, then `UPDATE automation_schedule SET enabled = TRUE WHERE name LIKE 'executor_pass%';`.
3. Set the repository variable `EXECUTOR_LIVE=1`. The morning plan then leaves `PROTECT_STOP`, `EXIT_TIME` and `EXIT_STOP` to the executor, so TRADE PLANS shows no duplicates.

Still unverified until the first live day: the response shapes of `place_*` and `get_trade_approvals`, and whether an approved order keeps its `ref_id`. If it doesn't, the next run halts, which fails safely.

**v1 vs v2 gate** (`compare_swing_v2.py`): v2 replaces v1 only with at least 30 closed trades per book and an average R at least as good. `--replay` adds the v2 screen replayed since swing mode began.

---

## Design principles

1. **Hard constraints live in Python; judgment lives in the model.** The model *proposes* an allocation and Python *disposes*. Base weights, regime gating, sector and theme caps, the cash floor, and the final clamp are all deterministic code. The model only refines weights inside those bounds. If its output doesn't sum to 100, the allocator falls back to the base weights.
2. **Rule-based where a rule is enough.** Market regime detection, signal-drift detection, the backtest, and the newsletter's "What Changed" diff use no LLM at all.
3. **Fail closed.** When the risk agent drops a ticker, that ticker is excluded (`risk_score=8`, `max_position_pct=0`, flagged `ASSESSMENT_MISSING`) instead of being sized as an ordinary position. A missing measurement shows as **N/A with a reason**, never as a zero that looks like "flat".
4. **Honest outputs.** When the constraint enforcer didn't run, the output says "not verified". It never shows a default "all clear". Columns added later default to `NULL` so older rows read "unknown" rather than holding made-up values.
5. **Wire up what exists before adding agents.** New work connects data the system already collects instead of adding more agents (see [IMPROVEMENTS.md](IMPROVEMENTS.md)).

---

## System architecture

```mermaid
flowchart TD
    subgraph S0["Stage 0 — Context (parallel)"]
        MAC[run_macro.py<br/>BLS / FRED CPI·PPI]
        ERN[run_earnings.py<br/>earnings calendar]
        REG[run_regime.py<br/>SPY·QQQ·VIX regime<br/>rule-based]
        FUN[run_fundamentals.py<br/>valuation snapshot]
        SMT[run_smart_money.py<br/>famous investors · SEC 13F / Form 4 / 13D·G]
    end

    subgraph S12["Stage 1+2 — Market data (parallel)"]
        OHLCV[pipeline.py<br/>OHLCV + metadata]
        OPT[run_options.py<br/>P/C, max pain, IV rank]
    end

    S0 --> S12
    S12 --> AN[Stage 3 — Analyst Agent<br/>run_agent.py · Claude<br/>BUY / SELL / HOLD per ticker]
    AN --> DR[Stage 3.5 — Drift<br/>run_drift.py<br/>signal flips, chronic flippers]
    DR --> RK[Stage 4 — Risk Manager<br/>run_risk.py · rules<br/>risk score + max position]
    RK -- Saturday only --> AL[Stage 5 — Allocator<br/>run_allocator.py<br/>base weights → Claude refine → enforcer]
    AL --> RW[Stage 6 — Report Writer<br/>Claude narrative]
    RW --> WD[Stage 7 — Watchdog<br/>rules consistency audit]
    WD --> NL
    RK -- swing days --> NL[Stage 8 — Newsletter<br/>HTML + plaintext email]
    NL --> BT[Stage 9 — Backtest<br/>score past plans]

    BT -. hit rates, alpha, drawdown .-> AN
    BT -. .-> RK
    DR -. chronic haircut .-> AL

    DB[(Supabase Postgres)]
    AN & RK & AL & RW & WD & BT --- DB
    DB --> DASH[React dashboard<br/>Vercel]
    DASH -- RUN PIPELINE --> API[/api/run-pipeline<br/>Vercel function/]
    API -- workflow_dispatch --> GHA[GitHub Actions<br/>orblex_pipeline.yml]
    GHA --> S0
```

---

## Pipeline stages

`run_all.py` orchestrates everything. Every stage runs as its own subprocess, and all stages share a single `run_id` (`pipeline-YYYY-MM-DD-xxxxxx`), so every row written during a run can be traced back to it.

| Stage | Script(s) | What it does | LLM |
|---|---|---|---|
| **−1** | `run_universe.py` | Swing only: picks today's tickers from the latest balance. On failure, uses the full list. | — |
| **0** | `run_macro.py`, `run_earnings.py`, `run_regime.py`, `run_fundamentals.py`, `run_smart_money.py` | Run in parallel. Macro inflation backdrop (BLS by default, FRED optional). Earnings dates, EPS surprise, and beat streak. Market regime classified from SPY/QQQ/VIX. Valuation fundamentals. Famous investors' holdings and recent trades from SEC EDGAR (see [Smart money](#smart-money)). | Macro summary only; the regime is purely rule-based |
| **1+2** | `pipeline.py`, `run_options.py` | Run in parallel. OHLCV ingestion into `stock_prices`. Options chain: put/call (volume and OI), max pain, top strikes, IV rank. | — |
| **3** | `run_agent.py` | Analyst Agent: one Claude call per ticker, returning signal, strength, sentiment score, valuation view, and anomalies | Yes |
| **3v2** | `run_swing_v2.py` | With `PIPELINE_V2=1`: the shadow book, script screen plus model veto, written only to `swing_v2_picks`. A failure is ignored. | Yes |
| **3.5** | `run_drift.py` | Compares today's signals with `signal_history` to find flips, scores their severity, and marks a ticker `is_chronic` when it flips ≥3 times in 7 days. Skipped in dry-run. | — |
| **4** | `run_risk.py` | Risk Manager, in `risk_rules.py`: `risk_score` 1–10, `max_position_pct`, risk flags, and a market risk level. `RISK_ENGINE=llm` rolls back to Claude | No |
| **5** | `run_allocator.py` | Weekly. Computes deterministic base weights, lets Claude refine them, then applies `enforce_allocation_constraints()` | Yes |
| **5b** | `build_longterm.py` | Weekly. Turns the allocation into `longterm_advice` | — |
| **6** | `run_report_writer.py` | Weekly. Synthesizes the analyst, risk, and allocation output into a narrative final report | Yes |
| **7** | `run_watchdog.py` | Weekly. Audits allocation and report with `watchdog_rules.py`. `WATCHDOG_ENGINE=llm` rolls back to Claude | No |
| **8** | `run_newsletter.py` | Builds the HTML and plaintext email (swing brief on weekdays, weekly review for `BOOK=longterm`) and sends it over SMTP to BCC recipients | Swing brief only |
| **9** | `run_backtest.py --summary` | Re-scores every past allocation plan against realized 1d/5d/20d returns and SPY. Runs last and can never block the newsletter. | — |

Orchestrator flags:

```bash
python run_all.py                     # full run for this book (swing skips stages 5–7)
python run_all.py --dry-run           # compute everything, write nothing, send nothing
python run_all.py --stages 0 1 2 3    # a subset (35 = drift)
python run_all.py --tickers NVDA AVGO # override the universe for this run
python run_all.py --skip-newsletter
python run_all.py --macro-source fred
```

The orchestrator (and every stage it launches) forces UTF-8 stdout, so model output containing characters like `≤`, `—`, or `≥` can't crash a Windows console. All log handlers are UTF-8 as well.

---

## The agents

Every Claude call goes through `llm.py`, which holds the model choice and each model's request rules. There are two routes:
- **Decision:** the analyst, the Saturday allocator and report writer, and the v2 ranking call run on `claude-sonnet-5-5` (`CLAUDE_DECISION_MODEL`), with adaptive thinking at low effort (`CLAUDE_DECISION_THINKING`) and server-side refusal fallback.
- **Fast:** earnings commentary, options-flow signal, macro summary and newsletter summary run on `claude-haiku-5-5` (`CLAUDE_FAST_MODEL`), with thinking off at low effort.

Replies are read by block type, and a refusal raises `ClaudeRefusal`. Setting either model variable back to `claude-sonnet-4-6` is a clean rollback.

The switch was made on 2026-10-12 from a 25-ticker analyst A/B on identical contexts. Sonnet 5.5 at adaptive-low cost $0.32 per run against $0.48 on Sonnet 4.6, with a median latency of 6s against 17s and 25/25 valid JSON. It also takes far more BUYs (9 vs 2), mostly pullback limit entries, which is what the swing prompt asks for. The swing backtest is the judge of whether those are better trades.

The project began on OpenAI `gpt-4o`, moved to `claude-sonnet-4-6`, and is now entirely on Claude 5.5 models.

### Analyst (`analyst_agent.py`, `context_builder.py`)
For each ticker, the analyst's context contains:
- **Technicals** computed from a 30-day lookback: RSI, MA5/MA20, annualized volatility, and last close
- **Fundamentals**, from the latest `stock_fundamentals` snapshot
- **News headlines** (NewsAPI) when `NEWS_ENABLED=true`
- **Prior decision:** the previous signal and strength, `held_sessions`, and `days_held`, read from the append-only `signal_history`. The prompt tells the analyst to value consistency and reverse a call only when it can name a material change.
- **Market context:** the regime line and one macro sentence, fetched once per run
- **Earnings:** days to earnings, event risk, last surprise, and beat streak. Within about 5 trading days of earnings, signal strength is capped at MODERATE.
- **Options flow:** put/call, IV rank, max pain, and a flow signal. Flow can raise or lower conviction but never reverses a call on its own.
- **Smart money:** which famous investors hold the ticker per their latest 13F and how they changed it last quarter, plus their Form 4 / 13D / 13G filings on it in the last 90 days. Treated as lagged context that can temper or support conviction; it never flips a call on its own.
- **Performance feedback:** this ticker's historical hit rate, plus portfolio alpha and drawdown (see [Feedback loop](#feedback-loop))

### Swing mode (`TRADING_MODE=swing`, `swing_policy.py`)
The analyst above reasons on a weeks-to-months horizon. Swing mode replaces it with a **1–10 trading day, long-only** trade planner. Enable it with `TRADING_MODE=swing` in `.env` (or the `TRADING_MODE` repository variable in GitHub Actions), or per run with `python run_agent.py --mode swing`. Apply `swing_schema.sql` first; until then the saver drops the plan columns and keeps the signal.

| | Position mode | Swing mode |
|---|---|---|
| Price history | 30 days, RSI/MA5/MA20 | 120 days: ATR(14), MA10/20/50 trend, prior 20-day high/low in ATRs, Bollinger %B, gap, close location, streak, relative strength vs SPY (5d/20d, SPY fetched live) |
| News | 7 days, by relevance | 3 days, newest first |
| Smart money | 13F holdings, Form 4/13D/13G, congressional PTRs | Form 4 from the last 14 days only |
| Valuation / CPI-PPI | Included | Dropped (no bearing on a 1–10 day hold) |
| Prior call | "Prize consistency" | Manage the prior BUY plan against its own stop/target; a prior HOLD carries no weight |
| Output | Signal + strength | Signal + `setup_type`, `entry_price` (limit, good for 2 sessions, from 2 ATR below to 0.5 ATR above the close), `stop_price`, `target_price`, `horizon_days`, `invalidation` |

Signals mean: **BUY** place the entry limit; **HOLD** no new entry, an open position keeps its levels; **SELL** the setup broke, exit (never a short).

After the model answers, `enforce_swing_rules()` validates the plan in Python and records every correction in `swing_flags` / `swing_notes` (the model's original call is kept in `model_signal`):
- **Stale prices**: a BUY is downgraded to HOLD if NYSE sessions are missing between the latest stored bar and today.
- **Earnings blackout**: a BUY is blocked when the report falls inside the hold, or its horizon is shortened so the position exits at least one session before the report.
- **Levels**: the entry is clamped to the allowed band; a missing or out-of-band stop (outside 0.5–4 ATR) falls back to 1.5 ATR; a missing target falls back to 2R; **reward:risk below 1.5 turns the BUY into a HOLD**.
- The horizon is clamped to 1–10 sessions.

**Swing backtest** (`swing_backtest.py`, run in Stage 9 alongside the allocation backtest, or alone with `python run_backtest.py --swing-only`): each swing BUY plan is simulated on daily bars as the account would trade it, and saved to `swing_backtest_trades`.
- **Entry**: the limit is live for 2 sessions. An open below it fills at the open; a low that touches it fills at the limit. An open at or below the stop cancels the order.
- **Fill day**: nothing exits on the fill day by default, so old and new plans stay comparable. A stop breached that day is acted on at the next session. The account may now exit that day; `fill_day_stop=True` scores that.
- **Exit**, from the next session on: a later swing SELL exits at the open; then the stop (a gap through it exits at the open); then the target; otherwise the close at `horizon_days`. A bar that touches both the stop and the target counts as a stop.
- **One trade per ticker**: a BUY while a trade or a resting entry is live is `SKIPPED_IN_TRADE`, and there is no re-buy on the exit session.
- **Scoring**: net of 5 bps per side, in % and in R (the planned risk, entry − stop, which is what the account sizes from). Statistics cover closed trades: win rate, expectancy (average R), profit factor, average hold and fill rate, broken down by setup, strength and ticker.
- **Feedback**: the swing analyst's backtest feedback is this ticker's and the book's closed-trade expectancy, replacing the 5d/20d hit rates.

Tests: `python -m pytest tests -q`.

**Trade plan and safety checks** (`trade_plan.py`, `trade_guard.py`, `run_trade_plan.py`; apply `trading_schema.sql` first). Nothing here places an order. The execution session reads the broker account into a snapshot JSON (format in `run_trade_plan.py`) and drives the CLI:

```bash
python broker_snapshot.py --portfolio p.json --positions pos.json --orders o.json --out snapshot.json
                                                            # Robinhood MCP responses → snapshot (unleveraged buying power)
python run_trade_plan.py plan --account snapshot.json      # → trade_orders, PROPOSED or BLOCKED
python run_trade_plan.py approve 42                         # your yes (or --reject)
python run_trade_plan.py check 42 --account snapshot.json --quote 227.10   # exit 0 = place it
python run_trade_plan.py placed 42 --broker-order-id abc123
python run_trade_plan.py fill 42 --qty 6 --price 227.40
python run_trade_plan.py halt --reason "..."   # kill switch for new entries; `resume` to undo
python run_trade_plan.py plan --paper 5000 --dry-run         # try it without a broker
```

`jobs.py` drives this every morning; see [A trading day](#a-trading-day).

**Universe from the account balance** (`run_universe.py`, `universe_selector.py`, `candidates.txt`; apply `universe_schema.sql`): in swing mode, Stage −1 runs before everything else. It reads the latest balance the morning task saved to `account_snapshots` (a table with no public read policy) and exports the day's list as `TICKERS` for every later stage.
- **HELD:** open positions, always analysed.
- **CORE:** `universe.txt` names the balance can size at least one whole share of. The rest are skipped with a reason such as "1 share needs a $1,136 account".
- **SLEEVE:** up to `UNIVERSE_SLEEVE_SIZE` (8) names from `candidates.txt` that pass the screen (price ≥ $5, ≥ $20M/day, ATR ≤ 6%, sizeable, uptrend). They are ranked by 20-day relative strength vs SPY, with at most 3 per sector.
- **Cost cap:** the total is capped at `UNIVERSE_MAX_TICKERS` (30), and the sleeve only fills the room the core leaves.
- **Fallback:** no balance in the last 7 days, or any failure, means the full universe as before.
- **Visibility:** each choice is saved to `universe_runs` and shown on TRADE PLANS. Sleeve tickers take their sector from `candidates.txt`, so they fall under the sector caps, and the trade guard accepts core plus candidates. Try a what-if balance with `UNIVERSE_EQUITY=2500 python run_universe.py --dry-run`.

**Robinhood price checks** (`price_checks.py`, applied by `plan --quotes`, required for a live account): every entry's latest stored close (Yahoo, via the pipeline) must be from the same session as Robinhood's official SIP close and within 0.5% of it, or the entry is blocked `BAD_PRICE_DATA`; a ticker with no quote is blocked too. The live price (the newer of the regular and extended-hours trade) blocks the entry when it is at or below the stop (`QUOTE_BELOW_STOP`), and is noted on every order against its limit. Close-based exits use Robinhood's official close when quoted.

Approvals go through `approve_trade_order()` and the kill switch through `set_trading_halt()` (`approval_schema.sql`). Both are Postgres functions that check the caller against `trading_admins` and only allow today's PROPOSED → APPROVED/REJECTED. Approved but not yet placed orders count against every cap in later plans and checks. `plan` builds one plan per day; `--replace` rebuilds it, expiring only unapproved orders. Only `rh_trader.py` can send an order, and only with the live switch on.

- **Entries**: today's swing BUYs, plus yesterday's unfilled ones unless today's report re-plans the ticker or says SELL. HOLD and SELL get no new money. Shares = 2.5% of equity × strength (STRONG 1, MODERATE 0.75, WEAK 0.5) × risk level ÷ (entry − stop).
  - Risk level = max(risk manager, regime floor): HIGH halves the risk; CRITICAL means no entries.
  - Size is capped at 30% of equity and at the risk manager's `max_position_pct`. `risk_score ≥ 8` is excluded.
  - Entries are ranked by strength, then reward:risk. Whole-share rounding and the 30% cap often keep realised risk well below the budget.
- **Exits**, for positions opened through the ledger, as market orders at the open:
  - SELL signal, a close at or below the stop, a close at or above the target, or `horizon_days` sessions completed.
  - Otherwise a resting GTC stop, from the fill day on. Broker positions with no ledger record are reported as UNMANAGED and left alone.
- **Guard**, run at plan time and again by `check` right before placing:
  - No re-buy of a ticker sold today, no 4th day trade in 5 sessions, and no order while an opposite-side order for the ticker is working. Today's fills come from both the broker snapshot and `trade_fills`, so either alone is enough to block.
  - At most 5 positions, 2 new entries a day, 30% of equity per position and 5% total open risk. Notional must fit the remaining buying power.
  - No new entries when halted, when today's loss reaches 3%, or when drawdown from the equity high-water mark reaches 10%. Exits are still allowed.
  - Entries must be limit orders with the stop below the limit, inside the 2-session plan window, in the universe, and not in a ticker already held. A live quote at or below the stop blocks the entry.
  - `check` also refuses any order not marked APPROVED and not dated today. A live plan refuses to run on a non-trading day or from a snapshot not taken today, and fails closed if the ledger tables are unreadable.

### Smart money (`run_smart_money.py`)
Tracks 11 famous US investors from SEC EDGAR (free, no API key): Buffett (Berkshire), Ackman (Pershing Square), Druckenmiller (Duquesne), Tepper (Appaloosa), Icahn, Cathie Wood (ARK), Loeb (Third Point), Klarman (Baupost), Soros, Coleman (Tiger Global), and Einhorn (Greenlight / DME Capital). Edit `INVESTORS` in the script to change the list.

| Filing | What it shows | How fresh |
|---|---|---|
| **13F-HR** | The fund's full long US equity book, diffed against the prior quarter (NEW / ADDED / REDUCED / SOLD) | Quarter-end snapshot, filed up to 45 days later |
| **Form 4** | Trades by >10% owners: shares, price, position after | Within 2 business days |
| **Schedule 13D / 13G** | New or changed >5% stakes; 13D signals activist intent | 5–10 business days |

13F lists CUSIPs only; they are mapped to tickers via OpenFIGI and cached in `cusip_ticker_map`. 13F amendments are applied (RESTATEMENT replaces the book, NEW HOLDINGS appends to it), and an investor can span several filer entities (Pershing moved its 13F to Pershing Square Inc. in 2026). The stage skips the 13F download when the latest filing has not changed, so daily runs make about one request per investor. **Congress** (`congress_ptr.py`): House members' STOCK Act Periodic Transaction Reports for Nancy Pelosi, Ro Khanna, Josh Gottheimer, and the twelve most active 2026 House filers (edit `CONGRESS_MEMBERS`). The House Clerk's yearly index lists each PTR; e-filed PTRs are PDFs with a text layer, parsed row by row (owner, ticker, stock vs options, buy/sell, trade date, amount range, description) into `investor_filings` with form `PTR`, so they flow into the dashboard feed, analyst context, and newsletter like any other filing. Caveats: trades are disclosed up to 45 days late and only as dollar ranges; rows in the smallest band ($1,001–$15,000) are skipped as managed-account noise; a report's trades in the same ticker and direction are merged into one row with summed ranges; the dashboard collapses any report with 4+ rows into one expandable line, and the newsletter shows at most 2 rows per report; paper filings are scanned images with no text (all of Khanna's are), so they appear as one "scanned, not machine-readable" row linking the PDF. Members have no SEC CIK and use negative ids (`--investors -1` runs Pelosi only). Senate PTRs are not covered.

### Risk Manager (`risk_rules.py`, `risk_context_builder.py`)
Rules since 2026-10-15, calibrated against 663 past model calls. The score starts from 30-day volatility, then adjusts for anomalies, SELL calls, earnings within 5 days, chronically wrong calls and the regime. A score of 8 or more excludes the ticker. `RISK_ENGINE=llm` rolls back to `risk_agent.py`, where any ticker the model leaves out fails closed (design principle 3).

### Allocator (`run_allocator.py`, `allocation_context_builder.py`, `allocation_saver.py`)
1. **Base weights (Python):**
   `score = SIGNAL_BASE[signal] × STRENGTH_MULT[strength] × risk_penalty(risk_score)`. SELL signals and `risk_score ≥ 8` are excluded. Chronic flippers lose one step of strength *for sizing only* (STRONG→MODERATE→WEAK).
2. **Effective risk level** = `max(risk manager level, regime risk floor)`, which sets the cash floor:
   LOW 5% · MODERATE 10% · HIGH 20% · CRITICAL 35%.
3. **Claude refines** the weights within these bounds. Tickers reporting earnings within 5 days are tagged `EARNINGS_IN=<n>d` and kept at or below their base weight.
4. **Enforcer clamp:** `instrument_policy.enforce_allocation_constraints()` applies the rules again after the model runs, and each correction is saved to `allocation_plans.constraint_violations`.

The saver is resilient: if the `base_weight` column migration hasn't been applied yet, it drops that one column and retries rather than failing the whole run.

### Report Writer and Watchdog
Both run in the Saturday book. The Report Writer turns the run into a structured, auditable final report (`final_reports`, `final_report_tickers`). The Watchdog (`watchdog_rules.py`) then audits it for internal consistency and writes the issues it finds to `watchdog_issues`.

---

## Constraints and the policy layer

`instrument_policy.py` defines what each ticker may do and when. Nothing in it is delegated to the model.

**Market regimes** (`regime_detector.py`, rule-based):

| Regime | Trigger (first match wins, most severe first) | Risk floor | Cash floor |
|---|---|---|---|
| BEAR | SPY drawdown ≤ −20%, OR SPY below its 200-day MA with VIX ≥ 30, OR a death cross with drawdown ≤ −15% | CRITICAL | ≥ 35%, 10% single-name cap |
| CORRECTION | drawdown ≤ −10%, OR SPY below its 200-day MA with VIX ≥ 25 | HIGH | ≥ 20% |
| CHOPPY | SPY or QQQ below its 200-day MA, OR VIX ≥ 20, OR drawdown ≤ −5% | MODERATE | ≥ 10% |
| BULL | none of the above | — | risk manager decides |

The regime can only raise the risk level, never lower it (`max()`, never `min()`).

**Instrument classes and caps:**
- `CORE`: eligible in every regime
- `DEFENSIVE` (XLP/XLU/GLD): CORRECTION and BEAR only, 12% per-ticker cap
- `INVERSE` (SH): BEAR only, 5% cap, force-exited after 10 consecutive runs held
- Sector caps: **SEMIS 30%**, **SOFTWARE 30%**. Theme cap: **TECH 80%**.

The defensive and inverse sleeves have since been removed from the tracked universe, but the policy for them is still in place. When a new ticker is added to `universe.txt`, it also needs a sector mapping in `instrument_policy.py`. Without one it falls into sector `OTHER` and escapes the concentration caps.

---

## Feedback loop

The system scores its own track record and feeds the result back into its decisions:

- **Stage 9 backtest** (`backtest_engine.py`) scores every past plan at 1, 5, and 20 trading days against SPY and an equal-weight benchmark.
  - A HOLD counts as correct when the move stayed within the ticker's own trailing 1-sigma expected move. (It used to be a fixed ±3% band.)
  - Metrics include a Sharpe-like ratio, max drawdown of the paper book, hit rate by signal strength, and net-of-cost return and alpha (5 bps per side).
  - SPY price gaps are backfilled against the portfolio's trading calendar.
- **`performance_context.py`** reads the latest backtest back into a compact summary: per-ticker hit rates, a pooled portfolio-wide hit rate, portfolio alpha, and current drawdown. The analyst prompt, the risk prompt, and the newsletter scorecard all read this *same* summary.
- **Chronic-flipper haircut:** any ticker flagged `is_chronic` by `run_drift` is mechanically sized down in Python.

---

## Dashboard

`src/Dashboard.jsx` is a React + Vite app behind a login panel (`src/LoginGate.jsx`). Only trading admins can sign in, and `private_access_schema.sql` makes the database refuse the public key on every table and view. Its tabs are grouped in the order data flows through the pipeline:

| Group | Views |
|---|---|
| 1 · MARKET | **REGIME** (SPY vs 200-day MA, drawdown, VIX, MA cross, rules fired) · **MACRO** |
| 2 · INPUTS | **EARNINGS** · **OPTIONS** (P/C, max pain, OI by strike, expiry tabs) · **SMART MONEY** (which famous investors own universe tickers, each investor's 13F book with quarter-over-quarter changes, latest Form 4 / 13D / 13G moves) |
| 3 · DECISIONS | **ANALYST** (price chart, signal, sentiment gauge, valuation chip, fundamentals, *Why this call?*) · **TRADE PLANS** (swing mode: each BUY's limit/stop/target/horizon with a stop→target price ladder, exit signals, rule overrides, today's `trade_orders`, simulated trades from `swing_backtest_trades`) · **RISK** · **ALLOCATION** (sector/theme exposure against caps, constraint-violations panel, sizing-method chips) |
| 4 · OUTPUT | **REPORT** |
| CHECKS | **DRIFT** · **WATCHDOG** · **PIPELINE** · **BACKTEST** |
| OPERATIONS | **AUTOMATION** · **DAILY REVIEW** (admins) |
| MAIN ACCOUNT | **LONG TERM** |

A regime chip in the header is visible on every tab.

**Signal story** (`src/SignalStory.jsx`): the "Why this call?" panel for a ticker explains the call in plain language:
1. **What the analyst looked at:** each input, the direction it points, and a link to the tab it came from
2. **How the analyst decided:** the prompt rules that applied to this ticker, followed by the analyst's own reasoning
3. **How big a position:** the deterministic sizing math with this ticker's real numbers, then the allocator's change against the base weight and any enforcer clamp

Clicking a ticker in any tab opens its story.

**RUN PIPELINE button** (`src/RunPipeline.jsx` and `api/run-pipeline.js`):
- You sign in with a Supabase magic link, and every read goes through that session.
- The Vercel function checks the session with Supabase Auth, checks the email against `PIPELINE_ADMIN_EMAILS`, rejects the request if a run is already in progress (409), and then dispatches the GitHub workflow.
- The GitHub token never reaches the browser.
- Options: dry run, skip newsletter, force run on a market holiday, and a specific stage list.
- The button shows the run's live status as QUEUED, then RUNNING, then its final result.

**Public demo** (`npm run dev:demo`, `npm run build:demo`, output in `dist-demo/`): the same dashboard with made-up tickers and numbers, opening on a HOW IT WORKS tab, with no login and no database. `vite.config.js` swaps the Supabase client for `src/demo/fakeSupabase.js`, which answers from `src/demo/fixtures.js`, so buttons work on an in-memory copy that resets on reload. `.env.demo` overrides `.env`, and `scripts/check_demo_bundle.mjs` fails the build if the production address or key, account numbers, personal emails or a real ticker reach the bundle. Deploy it as its own Vercel project: build command `npm run build:demo`, output directory `dist-demo`, and no `VITE_SUPABASE_*` variables.

**Build:** Vite splits the vendor code into `react`, `charts`, and `supabase` chunks. Price history is fetched newest-first with pagination, which works around PostgREST's 1000-row cap.

---

## Newsletter

`run_newsletter.py` sends one of two single-column HTML emails, each with a complete plaintext alternative, via SMTP with all recipients in BCC. `BOOK` picks the format, and each is deduplicated per day under its own `job_runs` name.

**Swing brief** (weekdays, `BOOK` unset or `daily`, job `newsletter`). Amber masthead `SWING BRIEF · 1–10 DAY HOLDS`, subject `Swing — N new entries, M exits — date`. Skipped when NYSE is closed. Sections, from top to bottom:

- **Today's counts**, with the Claude headline as one sentence under them
- **Market regime** strip, including a risk-floor badge
- **Swing trade plans:** each BUY's limit, stop, target, reward:risk and horizon with an indicative size for a `SWING_PREVIEW_EQUITY` account (default $5,000) at the per-trade risk ceiling, exit signals, BUYs the Python rules overrode and why, and the simulated closed-trade record
- **What changed:** computed in Python, not by the model. Signal flips (`FLIP`). Weight changes appear only when the run built an allocation plan, which swing days no longer do.
- **Risk watch**
- **Scorecard:** hit rate, alpha vs SPY, and paper-book drawdown. Uses the 20-day window and falls back to 5-day. Omitted until a window has matured.

Smart money, macro, the full signals table, earnings, sector exposure and constraint enforcement stay on the dashboard.

**Weekly review** (Saturday, `BOOK=longterm`, job `newsletter_longterm`). Teal masthead `WEEKLY REVIEW · MAIN ACCOUNT · ADVICE ONLY`, subject `Main account — N changes — week of Mon DD`. Built from the `longterm_advice` rows for `--run-id`, with no model call: the change count, one block per NEW / ADD / TRIM / EXIT (target vs last week's target, conviction, why, wrong if), the names that hold, and what changed since last week. It is advice for the main account, never an order.

```bash
python run_newsletter.py --dry-run --output-html                       # newsletter_swing_YYYY-MM-DD.html
BOOK=longterm python run_newsletter.py --dry-run --output-html --run-id longterm-...   # newsletter_longterm_YYYY-MM-DD.html
```

---

## Project structure

```
├── run_all.py                     ← orchestrator (stages 0–9)
├── universe.txt                   ← tracked tickers (source of truth, commented by sector)
├── config.py                      ← env loading/validation, universe resolution
├── instrument_policy.py           ← instrument classes, regime eligibility, caps, enforcer
│
├── # Stage 0 — context
├── run_macro.py                   ← BLS/FRED macro + Claude macro summary
├── run_earnings.py                ← earnings calendar
├── run_regime.py  regime_detector.py  regime_saver.py
├── run_fundamentals.py
├── run_smart_money.py             ← famous investors: SEC 13F + Form 4 + 13D/G
├── congress_ptr.py                ← House members' STOCK Act trades (PTR PDFs)
│
├── # Stage 1+2 — market data
├── pipeline.py  fetcher.py  loader.py  data_provider.py   ← yfinance | polygon
├── run_options.py
├── news_fetcher.py                ← NewsAPI headlines for the analyst
│
├── # Stage 3–5 — decisions
├── run_agent.py  analyst_agent.py  context_builder.py  report_saver.py  metrics_saver.py
├── run_drift.py
├── run_risk.py  risk_agent.py  risk_context_builder.py  risk_saver.py
├── run_allocator.py  allocation_context_builder.py  allocation_saver.py
│
├── # Stage 6–9 — output & checks
├── run_report_writer.py  report_writer_agent.py  report_writer_context_builder.py  report_writer_saver.py
├── run_watchdog.py  watchdog_agent.py  watchdog_context_builder.py  watchdog_saver.py
├── run_newsletter.py
├── run_backtest.py  backtest_engine.py  performance_context.py
│
├── scheduler.py                   ← optional long-running local scheduler (APScheduler)
│
├── schema.sql  agent_schema.sql  agent_schema_v2.sql
├── drift_schema.sql  fundamentals_schema.sql  pipeline_schema.sql  smart_money_schema.sql
│
├── src/
│   ├── Dashboard.jsx              ← main dashboard (all views)
│   ├── SignalStory.jsx            ← "Why this call?" per-ticker story
│   ├── SmartMoneyView.jsx         ← SMART MONEY tab
│   ├── BacktestView.jsx           ← BACKTEST view
│   ├── RunPipeline.jsx            ← RUN PIPELINE button + sign-in dialog
│   └── index.jsx
├── api/run-pipeline.js            ← Vercel serverless function → GitHub workflow_dispatch
├── index.html  vite.config.js  vercel.json  package.json
│
├── .github/workflows/orblex_pipeline.yml   ← pipeline CI (manual dispatch)
├── .github/PULL_REQUEST_TEMPLATE.md
├── IMPROVEMENTS.md                ← system review + improvement roadmap with status
└── requirements.txt
```

---

## Setup

### Prerequisites
- Python 3.10+ (CI uses 3.11)
- Node.js 18+
- A [Supabase](https://supabase.com) project
- An [Anthropic API key](https://console.anthropic.com)
- Optional: a NewsAPI key, a Polygon key, a FRED key, and SMTP credentials for the newsletter

### 1. Environment

```bash
cp .env.example .env
```

| Variable | Required | Purpose |
|---|---|---|
| `SUPABASE_URL` / `SUPABASE_KEY` | ✅ | Backend writes (`service_role` key) |
| `VITE_SUPABASE_URL` / `VITE_SUPABASE_PUBLISHABLE_KEY` | ✅ dashboard | Frontend reads (anon key) |
| `ANTHROPIC_API_KEY` | ✅ | All LLM stages |
| `TICKERS` | — | One-off universe override. Normally leave unset and edit `universe.txt`. |
| `HISTORICAL_DAYS` | — | History to pull on first ingest (default `90`) |
| `DATA_PROVIDER` / `POLYGON_API_KEY` | — | `yfinance` (default) or `polygon` |
| `NEWS_ENABLED` / `NEWS_PROVIDER` / `NEWSAPI_KEY` / `MAX_ARTICLES_PER_TICKER` | — | Headlines in the analyst context |
| `FRED_API_KEY` | — | Needed for `--macro-source fred` |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | newsletter | Email sending |
| `NEWSLETTER_FROM` / `NEWSLETTER_TO` | newsletter | Sender; comma-separated BCC recipients |
| `DASHBOARD_URL` | — | Link to the dashboard included in the newsletter |
| `SEC_USER_AGENT` | — | Name + contact email sent to SEC EDGAR, as SEC's fair-access policy asks (e.g. `Orblex Industries you@example.com`) |
| `OPENFIGI_API_KEY` | — | Raises the OpenFIGI CUSIP→ticker rate limit; only matters on the first smart-money run |
| `SCHEDULER_RUN_TIME` / `SCHEDULER_DRY_RUN` | — | Only for `scheduler.py` |

**Ticker universe:** `universe.txt` has one symbol per line, and `#` starts a comment. `TICKERS` in the environment overrides it. When neither is available, a built-in default list is used. The current universe has 32 names: semis and AI compute, enterprise software, AI cloud, mega-cap platforms, networking, power and nuclear, and robotics (including the ROBO and IBOT ETFs).

### 2. Database

In the Supabase **SQL Editor**, run these in order:

```
1. schema.sql              stock_prices, stock_metadata, pipeline_runs
2. agent_schema.sql        agent_reports, agent_run_log
3. agent_schema_v2.sql     risk_*, allocation_*, final_report*, watchdog_* (+ later column migrations)
4. drift_schema.sql        signal_history, signal_drift, signal_drift_summary view
5. fundamentals_schema.sql stock_fundamentals (+ agent_reports sentiment/valuation columns)
6. pipeline_schema.sql     pipeline_metrics
7. smart_money_schema.sql  smart_money_investors, investor_holdings, investor_filings, cusip_ticker_map (+ RLS read policies)
```

Run the trading, automation, and long-term schema files next, and `private_access_schema.sql` last, and again after running any other schema file. It makes the whole database readable only by signed-in trading admins, since the dashboard's public key ships in its JavaScript.

The DDL for `earnings_calendar`, `macro_indicators`/`macro_snapshots`, and `options_flow`/`options_strikes` is embedded in the docstrings of `run_earnings.py`, `run_macro.py`, and `run_options.py`.

> **Pending migration:** `agent_schema_v2.sql` adds `allocation_details.base_weight`. Run it, followed by `NOTIFY pgrst, 'reload schema';`, so that step 3 of the dashboard's signal story can show base weight and the allocator's change against it. Until then the pipeline still runs, and that step shows "unknown".

### 3. Dependencies

```bash
pip install -r requirements.txt
```

```bash
npm install
```

`yfinance` is pinned at `1.3.0`. Older versions don't impersonate a browser and get HTTP 429 from Yahoo on cloud runners.

---

## Running locally

```bash
python run_all.py --dry-run
```

```bash
python run_all.py
```

Individual stages all accept `--dry-run`. Examples:

```bash
python run_agent.py NVDA AVGO --dry-run
```

```bash
python run_backtest.py --summary
```

```bash
npm run dev
```

The dashboard is served at http://localhost:5173. The RUN PIPELINE button needs the `/api` function, so it only works when deployed on Vercel or when run locally with `vercel dev`.

---

## Deployment and CI

### Branches
- `develop` is where work lands
- `release` is production. Merging into it deploys the dashboard to Vercel, and dashboard-triggered pipeline runs use the code on this branch (`GITHUB_DISPATCH_REF`, default `release`).
- Other branches get Vercel preview deploys.

### Dashboard (Vercel)
`vercel.json` builds with Vite into `dist/` and rewrites every non-`/api` route to the SPA. Vercel environment variables:

| Variable | Purpose |
|---|---|
| `VITE_SUPABASE_URL`, `VITE_SUPABASE_PUBLISHABLE_KEY` | Frontend data access and auth |
| `GITHUB_TOKEN` | Fine-grained PAT for this repo only, with **Actions: read & write** |
| `PIPELINE_ADMIN_EMAILS` | Comma-separated allowlist of emails that can run the pipeline |
| `GITHUB_REPO`, `GITHUB_DISPATCH_REF` | Optional overrides |

Supabase Auth also has to allow magic-link sign-in and list the deployed URL as a redirect URL.

### Pipeline (GitHub Actions)
Supabase `pg_cron` (`automation_schema.sql`, `dispatch_due_jobs()`) checks every 5 minutes, in ET, and dispatches `.github/workflows/orblex_pipeline.yml` and `trading_jobs.yml` on `release`. GitHub's own cron is only a fallback for the pipeline, because it ran hours late. You can also start a run from the dashboard button or the Actions UI. Promote by fast-forwarding `release` to `develop`, through a PR you merge. The pipeline workflow:
- creates `.env` from repository secrets (`SUPABASE_*`, `ANTHROPIC_API_KEY`, `NEWSAPI_KEY`, `POLYGON_API_KEY`, `SMTP_*`, `NEWSLETTER_TO`, `NEWSLETTER_FROM`, and optionally `SEC_USER_AGENT`, `OPENFIGI_API_KEY`)
- skips the run when NYSE is closed (via `pandas-market-calendars`) unless `force_run` is set
- allows only one run at a time (`concurrency: orblex-pipeline`, queued rather than cancelled), with a 30-minute timeout
- uploads `*.log` as an artifact when a run fails

`scheduler.py` is an older alternative for an always-on machine, and is not in use.

---

## Database

| Area | Tables |
|---|---|
| Market data | `stock_prices`, `stock_metadata`, `stock_fundamentals`, `pipeline_runs`, `pipeline_metrics` |
| Context | `market_regimes`, `macro_indicators`, `macro_snapshots`, `earnings_calendar`, `options_flow`, `options_strikes` |
| Smart money | `smart_money_investors`, `investor_holdings` (latest 13F per investor, replaced when a new one lands), `investor_filings`, `cusip_ticker_map` |
| Analyst | `agent_reports`, `agent_run_log` |
| Drift | `signal_history` (append-only), `signal_drift`, `signal_drift_summary` (view) |
| Risk | `risk_assessments`, `risk_ticker_details` |
| Allocation | `allocation_plans` (with `sector_exposure`, `theme_exposure`, `constraint_violations`), `allocation_details` (with `sector`, `base_weight`) |
| Output | `final_reports`, `final_report_tickers`, `watchdog_logs`, `watchdog_issues` |
| Backtest | `backtest_runs`, `backtest_results`, `backtest_ticker_results` |

All writes are upserts on natural keys such as `(ticker, date)` and `(plan_run_id, ticker)`, so re-running a stage updates rows instead of duplicating them.

---

## Project history

| Phase | Highlights |
|---|---|
| **v1** | yfinance → Supabase data pipeline; single analyst agent (first Anthropic, then OpenAI `gpt-4o`); basic React dashboard |
| **Multi-agent** | Added the Risk Manager, Allocator, Report Writer, and Watchdog; `run_all.py` orchestrator; email newsletter; dashboard tabs for each agent |
| **Data breadth** | Macro (BLS/FRED), earnings, options flow, fundamentals, news (WSJ first, replaced by NewsAPI); swappable data provider for yfinance or Polygon; signal drift tracking; backtesting engine and BACKTEST view; GitHub Actions scheduler |
| **Regime and policy** | Rule-based market regime; `instrument_policy.py` with sector and theme caps and defensive/inverse sleeves; universe moved into `universe.txt`; all LLM stages moved to Claude |
| **Correctness pass** (IMPROVEMENTS Part 1) | Wired the policy layer into the live allocator, which previously never called it; applied the regime risk floor; made the constraint-enforcement reporting honest; merged the two allocator implementations; made the risk agent fail closed; UTF-8 logging everywhere |
| **Feedback loop** (Part 2) | Stage 9 backtest each run; performance-aware analyst and risk prompts; chronic-flipper haircut; analyst sees its own prior call; Stage 0 data wired into the analyst, risk, and allocator; IV rank now populated; better backtest metrics |
| **UX** (Part 3) | "What Changed" and Scorecard sections in the newsletter; per-ticker decision trace, which grew into the Signal Story; fix for price history being cut off at 1000 rows |
| **Production** | Dashboard on Vercel with vendor chunk splitting and a PR template; authenticated RUN PIPELINE button; workflow switched to manual-only; CI data-fetch fix (yfinance 1.3.0), newsletter secrets, and ticker-source fixes; universe refreshed to 25 names |

---

## Roadmap

[IMPROVEMENTS.md](IMPROVEMENTS.md) has the full list with findings and verification notes. Current status:

- ✅ **Part 1:** correctness fixes (all six)
- ✅ **Part 2:** feedback loop
- 🟡 **Part 3:** UX. Done: What Changed, decision trace / Signal Story, Scorecard, dashboard deployment. Pending: intraday alerts for threshold breaches, sent to Telegram or Discord.
- 🟡 **Part 4:** personal trading. Superseded by [EXECUTOR_DESIGN.md](EXECUTOR_DESIGN.md), whose §5 tracks each phase. Today: phase 1 (v2 shadow) and phase 4 (dry run) are running, and phase 5 (live executor) is built with its switch off.

**Non-goal:** adding more agents.
