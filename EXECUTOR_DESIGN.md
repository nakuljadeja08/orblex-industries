# Executor & Pipeline Redesign: Draft v1 (2026-10-15)

Status: **v1.2: decisions recorded through 2026-11-02 (§6)**. Sections 1–4 are the plan as written on 2026-10-15; §5 tracks what was built. Read it alongside `README.md`, which describes the running system.

**Built differently from this plan:**
- **No 60-second loop or heartbeat (§1.1, §1.4, §1.5).** The executor is a job: one pass at 9:25 ET, plus `executor_pass` at 10:30, 13:30 and 15:30 once the switch is on. It runs on GitHub Actions, not the user's machine, so `.trading/STOP` has no effect there; the dashboard HALT is the kill switch.
- **Robinhood token (§1.2).** Fernet-encrypted in `broker_secrets` for the cloud jobs, not only in Credential Manager.
- **Pipeline times (§1.1).** The pipeline runs at 6:30 ET, dispatched by `pg_cron`.
- **Long-term book (§2).** Runs Saturday, not Sunday.
- **Limits (§1.3).** See the note under the table.

This draft covers four requests:
1. An order executor with hard limits on how many trades it can make.
2. Splitting the short-term and long-term books so the swing pipeline stops reading
   long-horizon factors (smart money, 13F, valuation) that add noise at 1 to 10 days.
3. Deciding what a script should do versus what an LLM should do, to cut tokens and variance.
4. Whether to add inverse or leveraged ETFs for hedging.

---

## 0. Ground rules

- **The executor is the user's own program.** It runs on the user's machine or server, under
  the user's broker credentials, and the user starts and stops it. Claude writes and tests the
  code against paper accounts and dry runs. Claude never runs it against a live account and never
  submits live orders through the MCP.
- **Hard limits live in Python and fail closed.** No LLM output, config file or dashboard
  setting can loosen a code limit. The daily mandate (§1.3) can only tighten them.
- **Exits are always allowed** while entries are halted, subject to the day-trade rules.
- **Every order is idempotent.** The broker client order id is `wi-<trade_orders.id>`, so a
  retry, a crash-restart or a double run can never submit the same order twice.

---

## 1. The executor

### 1.1 Where it sits

```
 7:45  pipeline (GitHub Actions)   → agent_reports (swing)
 8:40  plan (run_trade_plan.py)    → trade_orders PROPOSED
       user, on the dashboard      → approves the DAILY MANDATE (§1.3) + any orders
 9:35  executor (user's process)   → for each order inside the mandate:
                                       reconcile → guard check with a live quote → submit
                                       ↳ Robinhood: lands in the in-app approval queue
                                         (human_must_approve_trades = true), user taps approve
 9:35–16:00 executor loop, every 60s → poll fills → record → manage exits/stops
16:35  EOD review (scheduled task) → log, alerts
```

The executor replaces only the "user types the order into the app" step. Plan generation, the
guard, the fill sync and the dashboard are reused as they are.

### 1.2 Broker adapters (one interface, three implementations)

```python
class Broker(Protocol):
    def account(self) -> AccountSnapshot: ...          # equity, buying power, positions, open orders, today's fills
    def quote(self, tickers) -> dict[str, Quote]: ...
    def submit(self, order: OrderTicket) -> BrokerOrder: ...   # idempotent on client_order_id
    def cancel(self, broker_order_id: str) -> None: ...
    def orders_since(self, ts) -> list[BrokerOrder]: ...
```

| Adapter | Use | Notes |
|---|---|---|
| `SimBroker` | unit tests and replays | Fills against stored daily bars or a recorded quote tape |
| `AlpacaBroker` | **paper first**, then live if chosen | API keys, bracket/OCO orders, intraday data, server-side PDT tracking |
| `RobinhoodBroker` | **chosen broker (decision 1)**, the agentic account | A Python MCP client (the `mcp` SDK) talking to the hosted endpoint `agent.robinhood.com/mcp/trading` over OAuth. Third-party guides report the OAuth flow works from a local client with a localhost redirect and needs no developer key. This is unconfirmed by Robinhood's docs, and one review reported problems, so **phase 0 is a read-only spike** (below). The token lives in the Windows Credential Manager (`keyring`), never in the repo or `.env`. **OCO exits are available** (`place_advanced_order`: a take-profit limit leg plus a stop-market leg on the same shares, GFD/GTC, regular hours only; each price ≥ 0.25% from market and the two ≥ $0.10 apart; idempotent via `ref_id`). The executor uses one OCO per position for stop + target instead of watching prices, which also fixes the "target only on close" gap in today's flow. There is still no entry bracket, so the OCO is placed after the entry fills. Orders land in the in-app approval queue (`human_must_approve_trades = true`), and pending approvals expire on their own. |

**Phase 0 result (2026-10-15): PASSED.** `rh_client.py` (commit 5721bb8) signed in through
the user's browser and phone approval, kept the token in Credential Manager, and read the account
(limited margin), portfolio, approval setting (`human_must_approve_trades = true`) and orders. The
server exposes 82 tools; the client allows 7 read tools. Other read tools worth adding later:
`get_equity_historicals` / `get_index_quotes` (Robinhood's own bars, which could replace Yahoo and
end the BAD_PRICE_DATA mismatches), `get_equity_tradability` (pre-order check) and
`get_advanced_orders` (OCO state).

**Phase 0 spike: Robinhood sign-in (read-only).** Write `rh_client.py` and confirm it can do
each of the following:
1. Run the OAuth login. The **user** completes it in the browser and confirms on the phone.
2. Store and refresh the token (Windows Credential Manager via `keyring`).
3. Call only `get_accounts`, `get_portfolio`, `get_equity_orders` and `get_trade_approval_setting`.

It contains no order code. If the spike fails, Robinhood stays on the current manual flow and we
revisit Alpaca.

### 1.3 Limiting how many trades it can make

Three layers. Each one can only make the one above it stricter.

**Layer 1: code ceilings** (`exec_limits.py`, constants with tests; changing one needs a commit):

| Limit | **Accepted (decision 2)** for a $500–5k account | Current code |
|---|---|---|
| New entries per day | **2** | 3 (`MAX_NEW_ENTRIES_DAY`) |
| New entries per rolling 5 sessions | **6** | none |
| Open positions | **3** | 5 |
| Orders submitted per day (all intents) | **8** | none |
| Re-submits of the same order | **1**, then it needs a human | none |
| Cancel/replace per order per day | **2** | none |
| Notional per order | 30% equity | 30% |
| Risk per trade | 1% equity × strength × level | same |
| Open risk, all positions | 5% equity | same |
| Day trades per rolling 5 sessions (while equity < $25k) | **3**, a hard block before the 4th | n/a (day trades banned today) |
| Consecutive stop-outs → no more entries today | **2** | none |
| Daily loss halt / drawdown halt | 3% / 10% | same |

**Changed since acceptance (the code in `exec_limits.py` is the source of truth):** open positions 3 → **5** (2026-10-22), risk per trade 1% → **2.5%** (2026-10-24, because a small book sized to 0 whole shares at 1%).

**Reconciled 2026-11-02: the numbers stay, and open risk is the limit that binds.** Two full-size STRONG trades use all of the 5%, so five positions fit only at about 1% each. In practice that holds on a small account: strength scaling and whole-share rounding size most entries well under 1% of equity. One full stop-out (2.5%) stays under the 3% daily halt, and a second trips it. See §4b for the replay at each open-position cap.

**Layer 2: the daily mandate.** The user approves it once each morning on the dashboard (a new
`trading_mandates` row through a SECURITY DEFINER RPC, like `approve_trade_order`):

```json
{ "date": "2026-10-16", "max_entries": 2, "max_total_risk_usd": 10,
  "allowed_tickers": ["ASX", "GTLB"], "order_types": ["LIMIT"],
  "require_stop": true, "expires": "16:00 ET" }
```

There is no mandate on a day the user skips, so the executor only manages exits for existing
positions.

**Layer 3: per-order approval.** This is optional, and on by default while we build trust:
- Robinhood: the in-app approval queue (already on).
- Alpaca: the dashboard APPROVE button, as today.

Turning it off is a later, explicit decision.

### 1.4 The executor loop (deterministic, no LLM)

```
startup:  load mandate (none → exits-only) · check kill switches · reconcile broker ↔ ledger
          any mismatch (untracked position, unknown order) → HALT + alert, do nothing else
every 60s during market hours:
  1. refresh account + quotes; stale quote (> 2 min) → skip the cycle
  2. sync fills (sync_fills logic, broker adapter instead of a JSON file)
  3. exits first: stop hit / target hit / time exit / exit signal → submit (if the guard allows)
  4. protective stops: every held position without a covering stop → submit at the plan stop
     (same day too, if the PDT counter allows it)
  5. entries: APPROVED orders inside the mandate, in rank order, until any limit is hit
     → run_trade_plan.check (guard + live quote) → submit → record PLACED
  6. unfilled entry past its window → cancel
  7. write a heartbeat row (dashboard shows "executor alive / last cycle")
```

### 1.5 Kill switches and failure modes

| Trigger | Action |
|---|---|
| Dashboard HALT ENTRIES (`trading_controls`) | No new entries, exits continue |
| `.trading/STOP` file exists | Executor exits after cancelling unfilled **entry** orders |
| Daily loss / drawdown / 2 stop-outs | Entries halted for the day |
| Broker ↔ ledger mismatch | Full halt + push alert, needs a human |
| Broker API error 3× in a row | Back off, alert, no blind retries |
| Executor heartbeat older than 5 min | EOD/fill-sync task alerts "executor down" |
| Crash mid-submit | On restart, look the order up by client order id before submitting again |

### 1.6 Day trading (decided 2026-11-02)

**Same-day exits are allowed, same-day re-buys are not.** `exec_limits.ALLOW_SAME_DAY_EXIT` is
on, for every order rather than per mandate, because exits are never gated by the mandate.
`SAME_DAY_REBUY` stays a hard rule. `trade_guard.py` replaces `SAME_DAY_SELL` with `PDT_LIMIT`:
a sell of a ticker bought today is refused when it could be the 4th day trade in 5 sessions under
$25k, counting stops already resting on other fills from today, or when the count can't be read. The
benefit is a protective stop on the fill day, not intraday scalping. At the limit a new fill stays
unprotected that night, and `fill_sync` emails STOP MISSING.

---

## 2. Split the books: short-term vs long-term

Agreed: more factors ≠ better. At a 1 to 10 day horizon, most fundamental and positioning data
moves too slowly to matter, and it gives the model extra things to rationalize.

| Factor | Swing book (1–10 days) | Long-term book (weeks–months) |
|---|---|---|
| Price/volume technicals, ATR, relative strength vs SPY/sector | **core** | context |
| Market regime (rule-based) | **gate** | **gate** |
| Earnings date | **hard blackout** | context |
| Recent news (≤3 days) | catalyst/veto only | context |
| Options flow (P/C, max pain) | **trial**: keep only if the ablation shows lift | drop |
| Insider Form 4 (fresh) | **trial** | **core** |
| 13F, congressional PTRs, smart money | drop | **core** |
| Valuation/fundamentals | drop | **core** |
| Macro CPI/PPI | drop (the regime already covers it) | **core** |
| Allocator weights | drop (sizing comes from risk per trade) | **core** |

**How the books run:**
- **Swing book:** runs daily, trades the agentic account, goes through the executor.
- **Long-term book:** runs **weekly** (Sunday) and is advisory only, for the main account, which
  the agent can't reach. Output is the newsletter and rebalance suggestions. Stages 0 (macro,
  fundamentals, smart money), 5 (allocator) and 8 (newsletter) move here.
- **Keep or drop each "trial" factor by evidence:** run `swing_backtest.py` with and without the
  factor on the same plans, and keep it only if expectancy (R/trade) improves by ≥ 0.05R across
  ≥ 50 trades.

### 2.1 Long-term book on the dashboard (decision 3)

The long-term book stays **advice only** for the main account, which the agent can only read.
Today its output is spread across ALLOCATION, RISK, SMART MONEY, MACRO and REPORT, so there is no
single place to read the advice. Proposal: one new top-level tab, **LONG TERM**, built to be read
in two minutes once a week.

```
LONG TERM · week of Oct 19 · regime BULL · next review Sun Oct 26
┌────────────────────────────────────────────────────────────────────────┐
│ THIS WEEK: 2 changes suggested · portfolio drift 6% from target        │
│ [ADD] MSFT +3%   [TRIM] NVDA −4%   everything else HOLD                │
└────────────────────────────────────────────────────────────────────────┘
 Holding   Now   Target   Action   Conviction   Why (≤3 plain bullets)        Wrong if
 NVDA     18%    14%      TRIM     ●●○          • 2 insiders sold (Form 4)     closes > 52w high
                                                 • valuation top decile
 MSFT      6%     9%      ADD      ●●●          • 13F adds by 4 funds           breaks 200-day MA
 ...
 [What changed since last week]  [Factor detail ▸]  [Copy rebalance list]
```

Design rules:
- **Lead with the decision.** The top card says what to do this week (add/trim/hold) and how far
  the portfolio has drifted. Detail is one click away under "Factor detail".
- **Now vs target.** "Now" comes from a weekly read-only snapshot of the main account (positions
  only, nothing else stored). "Target" comes from the allocator. Each row shows the gap as a
  bar.
- **Plain-language "why" with a confidence level.** At most 3 bullets, each tagged with its source
  (13F, Form 4, valuation, macro), plus a "wrong if" line, the same idea as `invalidation` in swing.
- **What changed since last week:** new or removed tickers, action flips and conviction changes.
  This replaces reading the whole newsletter.
- **Track record:** a small chart of past advice versus SPY, so the advice earns or loses trust over
  time.
- **Kept apart from swing.** Different colors, its own tab, and a "main account · advice only"
  label, so long-term advice is never confused with an order to place.

---

## 3. Script vs LLM

The rule: **a script does anything with a number or a rule; the LLM reads unstructured text
and makes vetoes.** The mechanical baseline (+0.21R/trade, PF 1.42, n=109) already shows the
script alone has an edge. The LLM's job is to beat it, and the backtest has to show that it does.

| Step | Today | Proposed |
|---|---|---|
| Universe | script | script |
| Indicators, setup detection (pullback/breakout) | LLM reads the numbers | **script** (`swing_policy` + the backtest's baseline rules) |
| Entry/stop/target | LLM proposes, script clamps | **script** (ATR rules), LLM may only tighten |
| News/catalyst read | LLM, per ticker | **Haiku, only on candidates that passed the screen** |
| Final pick/veto | LLM, per ticker (BUY/SELL/HOLD) | **one batched Sonnet call** ranking ≤ 6 candidates, with veto reasons |
| Risk score | LLM (1 call) | **done 2026-10-15:** `risk_rules.py`, calibrated to 663 past model calls (score within ±1: 81%; same 8+ exclusion: 87%). `RISK_ENGINE=llm` rolls back |
| Allocation | LLM (1 call) | script for swing; LLM only in the weekly long-term book |
| Watchdog | LLM (1 call) | **done 2026-10-15:** `watchdog_rules.py`, the 5 existing checks plus cross-agent, stance-rule and cash-note checks. `WATCHDOG_ENGINE=llm` rolls back |
| Report/newsletter | LLM daily | weekly, long-term book only |
| Execution, exits, sizing | script | script |

**Expected effect per daily run:**
- **Calls:** about 25 LLM calls today (19 analyst + risk + allocator + report + watchdog +
  newsletter + Haiku stages) drop to about 4–7 (≤6 Haiku news reads + 1 Sonnet ranking call).
- **Variance:** the same inputs give the same candidates, and the LLM only reorders or vetoes them.
- **Proof before switching:** for two weeks, log both "script-only" and "script + LLM veto" picks
  on the same days and compare R/trade, as with the earlier A/B on model routing.

---

## 4. Inverse ETFs: trading down markets (decision 4)

Decision: use inverse ETFs **in bear markets and on down days**, as a way to profit when the market
falls, **not as a standing hedge**. That framing fits a swing account well: they're ordinary
long-only buys (no shorting, no margin), with the same stop, size and hold rules as any other
trade.

**Instruments (whole-share friendly):**
- **1x:** SH (−1x S&P 500) and PSQ (−1x Nasdaq-100). PSQ fits better, since the long book is
  tech-heavy.
- **2x:** SDS and QID, at **half risk**, enabled only after 1x has passed its backtest.
- **No 3x** (SPXU/SQQQ): too much decay, and overnight gaps can jump straight past the stop.

**Two setups, both rule-based (scripts decide; the LLM can only veto on news):**

| Setup | When | Entry / exit | Size |
|---|---|---|---|
| **BEAR_TREND** | `regime_detector` = CORRECTION or BEAR | Normal swing rules on SH/PSQ: limit entry, ATR stop, ≤ 5-session hold | normal risk; 2x at half |
| **DOWN_DAY** | Regime BULL/NEUTRAL, but the index is breaking down: QQQ/SPY closes **below its 20-day MA and below the prior 10-day low** on above-average volume | Next-session limit entry, stop at the inverse ETF's equivalent of the breakdown day's index high, **≤ 3-session hold**, take profit at 1.5R | **half risk**, max 1 position |

Why DOWN_DAY needs a trend trigger and not just a red day: inside a bull trend, a single down day
often bounces the next session. Buying the inverse after one red close would mostly buy the low.
The backtest decides the exact trigger. The candidates are the 10-day-low break above, a 5-day
return below −3%, and a VIX spike above its 20-day average.

**Guardrails (in code):**
- An inverse entry and new long entries on the **same index exposure** never happen on the same
  day. That would just cancel out.
- Inverse ETFs count toward the 2/day and 6/week entry limits and the 3-position limit.
- At most **20% of equity** in inverse ETFs and at most 1 DOWN_DAY position. A regime flip back to
  BULL exits the BEAR_TREND trade at the next open.
- Extend the existing `INVERSE` class in `instrument_policy.py` to the swing path (today it only
  gates the position-mode allocator), and add PSQ, SDS and QID to it.

**Later, once day trading is confirmed:** an intraday DOWN_DAY variant (buy the morning
breakdown, sell before the close). It needs intraday data and uses one of the 3 PDT day trades,
so it comes after the swing version proves itself.

**Gate before live trading:** `swing_backtest.py` over a window that includes at least one real
correction. It needs ≥ 30 trades per setup with positive expectancy after costs, measured
separately for 1x and 2x.

---

## 4b. Phase 1 replay evidence (2026-10-15)

`swing_screen_backtest.py` ran the v2 rules day by day from 2022-01-03 to 2026-10-15. The
regime was rebuilt from SPY/QQQ/VIX (BULL 724 days, CHOPPY 198, CORRECTION 112, BEAR 166),
costs were 5 bps per side, and R was measured against the **planned** risk. The
"portfolio" view applies the account limits: 2 entries a day, 3 open (raised to 5 open on 2026-10-22). Caveats: today's ticker
lists are applied to the past, there is no historical earnings blackout, and the bars are daily.

**Inverse ETFs (§4): failed the gate as specified, so they are not traded.**

| Setup (every signal) | Trades | Avg R | PF |
|---|---|---|---|
| BEAR_TREND | 92 | −0.154 | 0.70 |
| DOWN_DAY | 102 | −0.187 | 0.72 |

In the portfolio view, DOWN_DAY reached +0.036R (n=72), which is break-even and not an edge.
Every ticker lost except QID in BEAR_TREND (+0.08R, n=24), which is too few trades to act on.

**Longs: the rule has a small edge, but only in bull markets.**
- Every signal: +0.077R (n=5,320). By regime: BULL +0.121R, CHOPPY −0.111R, CORRECTION −0.038R.
- The four optional filters change little (+0.073 to +0.097R), so none are switched on.
- Under the account limits:

| Variant | Trades | Avg R | PF |
|---|---|---|---|
| Rank by strength, all regimes | 435 | +0.013 | 1.02 |
| **Rank by strength, BULL only** | **315** | **+0.093** | **1.19** |
| Rank by pullback, all regimes | 386 | +0.049 | 1.10 |
| Rank by pullback, BULL only | 287 | +0.076 | 1.17 |

**Decision: longs only when the regime is BULL**, ranked by 20-day relative strength. If the
regime is unknown, the screen fails closed. The edge is small (about +0.09R a trade, roughly 65
trades a year), so the model's job in v2 is to prove that its vetoes add to it.

**Rerun 2026-11-02 at each open-position cap** (`--only longs --limits`, BULL only, rank by strength, 2022-01-03 to 2026-11-02). "Fill-day stops on" counts any fill-day low at or below the stop as stopped out, the worst case on daily bars:

| Open positions | Stops off: trades, avg R, PF | Stops on: trades, avg R, PF |
|---|---|---|
| 2 | 224, +0.103, 1.20 | 228, +0.068, 1.13 |
| 3 | 319, +0.089, 1.18 | 327, +0.068, 1.14 |
| 5 | 522, +0.075, 1.15 | 527, +0.106, 1.22 |

More slots add trades and total R (+23R, +29R, +39R with stops off) while average R slips, so 5 open remains defensible. Fill-day stops show no consistent effect: they hurt at 2 and 3 open and help at 5, which looks like path noise from which trades take the slots. They are justified by protecting the first night, not by this table. The earlier +0.093R no longer applies as a current figure: use +0.075R to +0.106R at 5 open, with the same caveats. The same limits with a $500 account over the last 30 sessions (`replay_executor.py`) gave 0 audit violations and 75 `OPEN_RISK_LIMIT` refusals, so open risk does bind at MODERATE size.

**Bug found on the way:** `swing_backtest.simulate_trade` measures R against the *fill*
(fill − stop). A gap-down fill just above the stop then shows ±16R to ±64R. The replay measures
against the planned risk instead. Fixed 2026-10-15: `simulate_trade` now measures R, MFE and
MAE against planned risk (entry − stop), and the replay's override is gone.

**Built (shadow, not trading):**
- `swing_screen.py`: the rules.
- `swing_llm_v2.py`: a fast-model news read per candidate with headlines, plus one ranking/veto
  call. The model can only veto, reorder or tighten a stop.
- `run_swing_v2.py`: writes `swing_v2_picks` only.
- `compare_swing_v2.py`: the A/B gate.

`run_all.py` runs it after stage 3 when `PIPELINE_V2=1`, and its failures never fail the run.
The first live dry run used 1,140 model tokens for the whole book.

## 5. Rollout (each phase has a gate)

| Phase | What | Gate to move on |
|---|---|---|
| 0 | Finish the test run (Oct 14–16); confirm PDT status; **Robinhood read-only sign-in spike** (§1.2) | a written verdict; the spike reads the account from our own script |
| 1 | Script/LLM split + book split behind `PIPELINE_V2=1`; old and new run side by side. **Backtest the inverse setups** (no live trades) | at least 30 closed trades per book (`compare_swing_v2.py`, changed from 2 weeks on 2026-11-02, when 2 weeks had given 1, 2 and 0); v2 ≥ v1 on R/trade |
| 1b | LONG TERM dashboard tab (§2.1) + weekly long-term run. **Built 2026-10-15**: Saturday 13:00 UTC position-mode run (`longterm-` run ids, no drift), `build_longterm.py` → `longterm_advice`, `save_holdings.py` (main-account % weights only, admin-read) → `longterm_holdings`, `src/LongTermView.jsx` | the week's advice is readable in ~2 min |
| 2 | `exec_limits.py` + mandate table/RPC + dashboard mandate card. **Built 2026-10-15**: `exec_limits.py` (ceilings, mandate, `check_submission` gate), `exec_state.py` (ledger → state), `executor_schema.sql` (`trading_mandates` + `set_/revoke_trading_mandate`, `exec_events`), mandate card on TRADE PLANS; `trade_guard` now imports its caps from `exec_limits` (2 entries/day, 3 open apply to the manual plan too) | tests cover every limit and fail closed: **met** (`tests/test_exec_limits.py`, 23 tests) |
| 3 | Executor with `SimBroker`, replaying the last 30 sessions. **Built 2026-10-15**: `broker.py` (interface + SimBroker with OCO, idempotent client ids, failure injection), `executor.py` (reconcile → exits → entries, every order through `check_submission`, crash recovery by client id, broker-error lockout per day), `replay_executor.py` (30 real sessions under stress + an independent audit) | **met**: 0 violations at $5,000 and $500 (2026-09-03 → 10-15), both crash points recovered with no duplicate orders, 13 executor tests |
| 4 | Executor in **dry-run on Robinhood**: it builds and checks orders against live quotes and logs what it *would* submit, for 2 weeks, compared with what the user actually placed. **Started 2026-10-15**: `robinhood_broker.py` (read-only adapter + `DryRunBroker`), `run_dryrun.py` (ledger snapshot, never writes trade tables), `compare_dryrun.py`, `exec_dryrun` table; tasks `executor-dryrun` (9:25 ET) and the EOD comparison. First live run: **would HALT**: two orders placed by hand at limits above the approved plan were never linked, so the ledger missed them; `sync_fills` now names such price near-misses | zero limit violations; its picks match the manual ones |
| 5 | Live on Robinhood at the smallest size. Orders go to the **in-app approval queue**; the user taps approve. **Built 2026-10-20, switch OFF**: `rh_trader.py` (the only module allowed to send; refuses unless `EXECUTOR_LIVE=1`, the agentic account, and `human_must_approve_trades = true` read in the same session), `LiveRobinhoodBroker` (OCO groups, pending approvals, `ref_id = uuid5("wi-<id>")` for idempotency and linking), `run_executor.py` (dashboard-APPROVED entries only, dashboard mandate or no entries, ledger written through to Supabase), `sync_fills` links by `ref_id`. The `dryrun` job runs it instead of the dry run when the repo variable `EXECUTOR_LIVE=1`. Unconfirmed until the first live day: the exact response of `place_*` and of `get_trade_approvals`, and whether an approved order keeps our `ref_id`; if not, the next run sees it as untracked and HALTS (fails closed). **Added 2026-11-02**: the plan leaves `PROTECT_STOP`, `EXIT_TIME` and `EXIT_STOP` to the executor when the switch is on; `executor_pass` rows (disabled until go-live) catch approvals after 9:25 and protect same-day fills | phase 4 gate passed with real mandates + `executor_pass` rows enabled + user sets `EXECUTOR_LIVE=1` |
| 6 | Inverse ETF setups live (1x first, then 2x) | the §4 backtest gate is passed |

## 6. Decisions (recorded 2026-10-15)

1. **Broker: Robinhood for now** (no bandwidth for a second broker's setup). Alpaca is the fallback
   if the phase 0 sign-in spike fails or the approval queue proves too limiting.
2. **Trade limits: accepted** as in §1.3.
3. **Long-term book: advice only** for the main account, with a dedicated LONG TERM dashboard tab
   (§2.1).
4. **Inverse ETFs: yes, for bear markets and down days** as profit-seeking trades (§4), backtested
   first. 1x before 2x, no 3x.

5. **Day trading (2026-11-02):** same-day exits allowed, same-day re-buys not (§1.6).
6. **v1 vs v2 gate (2026-11-02):** count closed trades, at least 30 per book (§5).
7. **Limits (2026-11-02):** keep 2.5%, 5% and 5; open risk binds (§1.3).
8. **Allocator, report, watchdog (2026-11-02):** weekly, in the long-term book (§3). Macro, 13F and valuation still run daily.
