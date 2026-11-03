"""
replay_executor.py — the phase 3 gate (EXECUTOR_DESIGN.md §5): run the executor against
SimBroker over the last N real sessions, under stress, then AUDIT every limit from the
broker's own records (not the executor's bookkeeping).

Each session d:
  plan     the v2 rules (swing_screen, BULL only) on closes through the prior session,
           TOP 4 candidates sized by trade_plan.size_entry and marked APPROVED (as if the
           user had approved them) — twice the 2-a-day limit on purpose
  mandate  top 3 tickers, max 2 entries, risk budget = the top 2's risk
  stress   every 4th session a rogue order outside the mandate and a MARKET entry;
           one HALT day; one broker-outage day (first submit fails → RESUBMIT);
           one crash AFTER the broker accepted an order and one crash BEFORE submit,
           each followed by a fresh executor (restart) on the same broker + ledger
  run      executor.pre_open twice (the loop runs every minute; the second pass must
           be a no-op apart from retries) → SimBroker.run_session → post_close

Audit (must all be zero): entries/day > 2, entries/5 sessions > 6, orders/day > 8,
submits per order > 2, positions + working entries > MAX_OPEN_POSITIONS, same-day re-buys, entries
outside the mandate or over its entry count or risk budget, entries while halted,
positions held overnight without a working OCO, duplicate client order ids at the broker.

Usage:
  python replay_executor.py [--sessions 30] [--equity 5000]
"""

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from broker import SimBroker
from exec_limits import (MAX_ENTRIES_5_SESSIONS, MAX_NEW_ENTRIES_DAY, MAX_OPEN_POSITIONS, MAX_ORDERS_DAY,
                         MAX_RESUBMITS, Mandate)
from executor import Executor, MemoryLedger, SimulatedCrash
from swing_policy import compute_swing_indicators
from swing_screen import rank, screen_long
from swing_screen_backtest import IND_BARS, _tickers, load_bars, regime_on
from trade_plan import size_entry

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

CANDIDATES_PER_DAY = 4


def plan_for(bars: dict, tickers: list[str], d_prev: date, equity: float) -> tuple[list[dict], str]:
    regime = regime_on(bars, d_prev)
    spy_w = bars["SPY"][bars["SPY"]["date"] <= d_prev].tail(IND_BARS)
    cands = []
    for t in tickers:
        df = bars.get(t)
        if df is None:
            continue
        w = df[df["date"] <= d_prev].tail(IND_BARS)
        if len(w) < 60 or w["date"].iloc[-1] != d_prev:
            continue
        p, _ = screen_long(t, compute_swing_indicators(w, spy_w), regime)
        if p:
            qty, risk = size_entry({**p, "signal_strength": "MODERATE"}, equity, "MODERATE", None)
            if qty > 0:
                cands.append({**p, "qty": qty, "risk_dollars": risk})
    return rank(cands)[:CANDIDATES_PER_DAY], regime


def bar_dict(bars: dict, d: date) -> dict:
    out = {}
    for t, df in bars.items():
        row = df[df["date"] == d]
        if not row.empty:
            r = row.iloc[0]
            out[t] = {"open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"])}
    return out


def run(n_sessions: int, equity: float) -> int:
    tickers = sorted(set(_tickers("universe.txt") + _tickers("candidates.txt")))
    spy_dates = None
    start = date.today() - timedelta(days=int(n_sessions * 1.6) + 10)
    print(f"Loading bars for {len(tickers)} tickers …")
    bars = load_bars(tickers + ["SPY", "QQQ", "^VIX"], start)
    spy_dates = [d for d in bars["SPY"]["date"]]
    sessions = spy_dates[-n_sessions:]
    calendar = spy_dates                                   # full calendar for horizon counting
    stress_halt = sessions[len(sessions) // 3]
    stress_outage = sessions[len(sessions) // 2]
    crash_after = sessions[len(sessions) // 2 + 3]
    crash_before = sessions[len(sessions) // 2 + 6]

    broker = SimBroker(cash=equity)
    ledger = MemoryLedger()
    ex = Executor(broker, ledger, calendar)
    audit_open = []                 # (d, positions + working entries) after pre_open
    audit_unprotected = []
    mandates = {}
    restarts = 0

    for i, d in enumerate(sessions):
        d_prev = calendar[calendar.index(d) - 1]
        eq = broker.account().equity
        cands, regime = plan_for(bars, tickers, d_prev, eq)
        for k, c in enumerate(cands):
            ledger.add_order({"plan_date": d.isoformat(), "report_date": d, "ticker": c["ticker"], "side": "BUY",
                              "intent": "ENTRY", "order_type": "LIMIT", "qty": c["qty"], "limit_price": c["entry_price"],
                              "stop_price": c["stop_price"], "target_price": c["target_price"],
                              "horizon_days": c["horizon_days"], "risk_dollars": c["risk_dollars"], "rank": k})
        if i % 4 == 3 and cands:                                       # adversarial approvals
            c = cands[0]
            ledger.add_order({"plan_date": d.isoformat(), "report_date": d, "ticker": "ROGUE", "side": "BUY",
                              "intent": "ENTRY", "order_type": "LIMIT", "qty": 1, "limit_price": 10.0, "stop_price": 9.0,
                              "target_price": 12.0, "horizon_days": 5, "risk_dollars": 1.0, "rank": -2})
            ledger.add_order({**{k: v for k, v in ledger.orders[max(ledger.orders)].items() if k not in ("id", "status")},
                              "ticker": c["ticker"], "order_type": "MARKET", "rank": -1})
        m = Mandate(mandate_date=d, max_entries=2, max_total_risk_usd=round(sum(c["risk_dollars"] for c in cands[:2]), 2),
                    allowed_tickers=frozenset(c["ticker"] for c in cands[:3]),
                    expires_at=datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc).replace(hour=20))
        ledger.mandates[d] = mandates[d] = m

        ledger.halted = d == stress_halt
        if d == stress_outage:
            broker.fail_next = 1
        if d in (crash_after, crash_before):
            ex.crash_at = "after_submit" if d == crash_after else "before_submit"

        for _pass in range(2):
            try:
                ex.pre_open(d)
            except SimulatedCrash:
                restarts += 1
                ex = Executor(broker, ledger, calendar)        # restart: fresh process, same broker + ledger
                ex.pre_open(d)
        ledger.halted = False

        acct = broker.account()
        working_buys = {o.ticket.ticker for o in broker.orders() if o.state == "WORKING" and o.ticket.side == "BUY"}
        audit_open.append((d, len(set(acct.positions) | working_buys)))
        oco = {o.ticket.ticker for o in broker.orders() if o.state == "WORKING" and o.ticket.side == "SELL"}
        for t, mg in ledger.managed().items():
            if mg["opened_date"] < d and t not in oco:
                audit_unprotected.append((d, t))

        broker.run_session(d, bar_dict(bars, d))
        ex.post_close(d)

    return audit(broker, ledger, ex, sessions, mandates, audit_open, audit_unprotected, stress_halt, restarts, equity)


def audit(broker, ledger, ex, sessions, mandates, audit_open, audit_unprotected, stress_halt, restarts, equity) -> int:
    v = []
    ok_events = [e for e in ledger.events if e["kind"] in ("SUBMIT", "RESUBMIT")]
    by_day = defaultdict(list)
    for e in ok_events:
        by_day[e["trade_date"]].append(e)
    for d in sessions:
        ds = d.isoformat()
        entries = {e["ticker"] for e in by_day[ds] if e["intent"] == "ENTRY" and e["ok"]}
        if len(entries) > MAX_NEW_ENTRIES_DAY:
            v.append(f"{ds}: {len(entries)} entries > {MAX_NEW_ENTRIES_DAY}")
        win = sessions[max(0, sessions.index(d) - 4): sessions.index(d) + 1]
        wk = sum(len({e["ticker"] for e in by_day[x.isoformat()] if e["intent"] == "ENTRY" and e["ok"]}) for x in win)
        if wk > MAX_ENTRIES_5_SESSIONS:
            v.append(f"{ds}: {wk} entries in 5 sessions > {MAX_ENTRIES_5_SESSIONS}")
        n_orders = sum(1 for e in ledger.events if e["trade_date"] == ds and e["kind"] in ("SUBMIT", "RESUBMIT", "REPLACE"))
        if n_orders > MAX_ORDERS_DAY:
            v.append(f"{ds}: {n_orders} orders > {MAX_ORDERS_DAY}")
        m = mandates[d]
        for e in by_day[ds]:
            if e["intent"] == "ENTRY" and e["ok"] and e["ticker"] not in m.allowed_tickers:
                v.append(f"{ds}: entry {e['ticker']} outside the mandate")
        risk = sum(float(e["risk_dollars"] or 0) for e in by_day[ds] if e["intent"] == "ENTRY" and e["ok"])
        if risk > m.max_total_risk_usd + 1e-6:
            v.append(f"{ds}: entry risk ${risk:.2f} > mandate ${m.max_total_risk_usd:.2f}")
        if len(entries) > m.max_entries:
            v.append(f"{ds}: {len(entries)} entries > mandate {m.max_entries}")
        if d == stress_halt and entries:
            v.append(f"{ds}: entries submitted while halted: {sorted(entries)}")
    per_order = defaultdict(int)
    for e in ok_events:
        per_order[e["order_id"]] += 1
    v += [f"order {oid}: {n} submits > {1 + MAX_RESUBMITS}" for oid, n in per_order.items() if n > 1 + MAX_RESUBMITS]
    v += [f"{d}: {n} positions + working entries > {MAX_OPEN_POSITIONS}" for d, n in audit_open if n > MAX_OPEN_POSITIONS]
    sold = set()
    for f in broker.fills_log:                             # in fill order: a sell, then a buy back
        if f["side"] == "SELL":
            sold.add((f["ticker"], f["date"]))
        elif (f["ticker"], f["date"]) in sold:
            v.append(f"{f['date']}: same-day re-buy of {f['ticker']}")
    v += [f"{d}: {t} held overnight without a working stop" for d, t in audit_unprotected]
    cids = [o.ticket.client_order_id for o in broker.orders()]
    if len(cids) != len(set(cids)):
        v.append("duplicate client order ids at the broker")
    linked = [o["broker_order_id"] for o in ledger.orders.values() if o.get("broker_order_id")]
    if len(linked) != len(set(linked)):
        v.append("two ledger orders linked to one broker order")
    recon = ex.reconcile()
    v += [f"final reconcile: {p}" for p in recon]

    # ── report ──
    acct = broker.account()
    refusal_counts = defaultdict(int)
    for _, _, _, _, reasons in ledger.refusals:
        for r in reasons:
            refusal_counts[r] += 1
    entries_ok = [s for s in ledger.submitted if s[3] == "ENTRY" and s[5]]
    exits = defaultdict(int)
    for f in ledger.fills:
        if f["side"] == "SELL":
            exits[f.get("exit_intent") or ledger.orders[f["order_id"]]["intent"]] += 1
    print(f"\nReplay {sessions[0]} → {sessions[-1]} ({len(sessions)} sessions), start equity ${equity:,.0f}")
    kinds = defaultdict(int)
    for e in ledger.events:
        kinds[(e["kind"], e["ok"])] += 1
    recovered = [e for e in ledger.events if e.get("detail") == "recovered after restart"]
    print(f"  orders in ledger {len(ledger.orders)} · submitted {len(ledger.submitted)} "
          f"(entries {len(entries_ok)}) · broker submit() calls {broker.submit_calls} · broker orders {len(broker.orders())}")
    print(f"  fills {len(broker.fills_log)} · exits {dict(exits)} · restarts after crashes {restarts}")
    event_counts = {(k if ok else k + " (failed)"): n for (k, ok), n in kinds.items()}
    print(f"  events {event_counts} · "
          f"recovered after restart: {[(e['trade_date'], e['ticker']) for e in recovered]}")
    print(f"  refusals by reason: {dict(sorted(refusal_counts.items(), key=lambda kv: -kv[1]))}")
    print(f"  alerts: {ledger.alerts or 'none'}")
    print(f"  end equity ${acct.equity:,.2f} ({(acct.equity / equity - 1) * 100:+.2f}%), open positions {sorted(acct.positions)}")
    print(f"\nAUDIT: {len(v)} violation(s)")
    for x in v:
        print(f"  ✗ {x}")
    if not v:
        print("  ✓ every limit held, no duplicates after crash/restart, ledger and broker reconcile")
    return 0 if not v else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay the executor on SimBroker and audit every limit")
    ap.add_argument("--sessions", type=int, default=30)
    ap.add_argument("--equity", type=float, default=5000.0)
    args = ap.parse_args()
    return run(args.sessions, args.equity)


if __name__ == "__main__":
    sys.exit(main())
