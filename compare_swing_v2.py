"""
compare_swing_v2.py — the phase 1 gate (EXECUTOR_DESIGN.md §5): over the days both
pipelines ran, simulate three books on the same bars and compare R per trade.

  v1          swing BUY plans in agent_reports (the analyst model sets the levels)
  v2-script   swing_v2_picks with script_pick = true (rules only, no model)
  v2-llm      swing_v2_picks with status = PICK (rules + news read + model veto)

Each book is capped at 2 entries per day (v1: its 2 strongest plans by
signal_strength then reward:risk, as the morning plan would size them).
R is measured against the planned risk (entry − stop), as in swing_backtest.simulate_trade.

The gate counts closed trades, not weeks (decided 2026-11-02): each book needs at least
MIN_CLOSED_PER_BOOK, because a fill rate near 40% leaves only a handful after two weeks.
--replay reaches that sooner for the rules: it runs the v2 screen over history (v2-script
only, the model's vetoes cannot be replayed) against every v1 plan since swing mode began.

Usage:
  python compare_swing_v2.py [--since 2026-10-16] [--replay]
"""

import argparse
import sys
from datetime import date

from config import load_config
from loader import get_client
from swing_backtest import _fetch_bars, _fetch_swing_reports
from swing_screen_backtest import _simulate, stats

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

STRENGTH = {"STRONG": 0, "MODERATE": 1, "WEAK": 2}
PER_DAY = 2
MIN_CLOSED_PER_BOOK = 30


def gate_verdict(v1: dict, v2: dict) -> str:
    """stats() of v1 and of one v2 book → the gate's answer. Pure."""
    short = [f"{name} {s['closed']}/{MIN_CLOSED_PER_BOOK}" for name, s in (("v1", v1), ("v2", v2))
             if s["closed"] < MIN_CLOSED_PER_BOOK]
    if short:
        return "INCONCLUSIVE: closed trades " + ", ".join(short)
    if v2["avg_R"] >= v1["avg_R"]:
        return f"V2 PASSES: {v2['avg_R']:+.3f}R vs v1 {v1['avg_R']:+.3f}R"
    return f"KEEP V1: v2 {v2['avg_R']:+.3f}R vs v1 {v1['avg_R']:+.3f}R"


def _v1(sb, days: set) -> list[dict]:
    plans = [r for r in _fetch_swing_reports(sb) if r["signal"] == "BUY" and r["report_date"] in days
             and all(r.get(k) is not None for k in ("entry_price", "stop_price", "target_price", "horizon_days"))]
    out = []
    for d in sorted(days):
        day = sorted([p for p in plans if p["report_date"] == d],
                     key=lambda p: (STRENGTH.get(p.get("signal_strength"), 3), -(p.get("risk_reward") or 0)))
        out += day[:PER_DAY]
    return out


def _v2(sb, since: date) -> list[dict]:
    rows = (sb.table("swing_v2_picks").select("*").gte("report_date", since.isoformat())
            .order("created_at", desc=True).execute().data or [])
    latest = {}
    for r in rows:   # latest run per day wins
        latest.setdefault(r["report_date"], r["run_id"])
    rows = [r for r in rows if latest[r["report_date"]] == r["run_id"]]
    for r in rows:
        r["report_date"] = date.fromisoformat(str(r["report_date"])[:10])
        for k in ("entry_price", "stop_price", "target_price"):
            r[k] = float(r[k]) if r[k] is not None else None
    return rows


def _book_stats(plans: list[dict], bars) -> dict:
    trades = []
    for p in plans:
        tb = bars[bars["ticker"] == p["ticker"]].sort_values("date").reset_index(drop=True)
        if not tb.empty:
            trades.append(_simulate(p, {p["ticker"]: tb}))
    return stats(trades)


def _print_book(name: str, n_plans: int, s: dict) -> None:
    f = lambda v, fmt: "—" if v is None else format(v, fmt)
    print(f"  {name:<24} plans {n_plans:>3}  closed {s['closed']:>3}  fill {f(s['fill_rate'], '.0%'):>4}  "
          f"win {f(s['win_rate'], '.0%'):>4}  avg {f(s['avg_R'], '+.3f'):>7}R  total {s['total_R']:>+6.1f}R")


def _replay(sb) -> None:
    """v2 rules replayed over every session since v1's first swing plan, against all v1 plans."""
    from swing_screen_backtest import _tickers, generate_signals, load_bars, precompute
    reports = _fetch_swing_reports(sb)
    days = {r["report_date"] for r in reports}
    since = min(days)
    longs = sorted(set(_tickers("universe.txt") + _tickers("candidates.txt")))
    bars = load_bars(longs + ["SPY", "QQQ", "^VIX"], since)
    longs = [t for t in longs if t in bars]
    signals, _ = generate_signals(bars, since, longs, [], (), precompute(bars, since, longs), None, "rs", {"BULL"})
    picks = [p for p in signals if p["day_rank"] < PER_DAY]
    replayed = stats([_simulate(p, bars) for p in picks])
    v1_plans = _v1(sb, days)
    tickers = sorted({p["ticker"] for p in v1_plans})
    v1_all = _book_stats(v1_plans, _fetch_bars(sb, tickers, since)) if tickers else stats([])
    print(f"\nSince swing mode began ({since}), simulated:")
    _print_book("v1 (analyst model)", len(v1_plans), v1_all)
    _print_book("v2 screen, replayed", len(picks), replayed)
    print("  Replay caveats: today's ticker lists applied to the past; no earnings blackout.")
    print(f"  Gate: {gate_verdict(v1_all, replayed)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare v1 swing plans with v2 shadow picks")
    ap.add_argument("--since", default="2026-10-16")
    ap.add_argument("--replay", action="store_true", help="also replay the v2 screen since swing mode began")
    args = ap.parse_args()
    since = date.fromisoformat(args.since)
    cfg = load_config()
    sb = get_client(cfg.supabase_url, cfg.supabase_key)

    v2 = _v2(sb, since)
    days = {r["report_date"] for r in v2}
    if not days:
        print(f"No swing_v2_picks since {since}: is PIPELINE_V2=1 set and swing_v2_schema.sql applied?")
        return 1
    books = {
        "v1 (analyst model)":    _v1(sb, days),
        "v2 script only":        [r for r in v2 if r["script_pick"]],
        "v2 script + model veto": [r for r in v2 if r["status"] == "PICK"],
    }
    tickers = sorted({p["ticker"] for b in books.values() for p in b})
    bars = _fetch_bars(sb, tickers, since) if tickers else None
    print(f"{len(days)} day(s) with both pipelines since {since}\n")
    results = {}
    for name, plans in books.items():
        results[name] = _book_stats(plans, bars) if tickers else stats([])
        _print_book(name, len(plans), results[name])
    vetoed = [r for r in v2 if r["status"] == "VETO"]
    print(f"\n  model vetoes: {len(vetoed)}" + "".join(f"\n    {r['report_date']} {r['ticker']}: {r['llm_reason']}" for r in vetoed[:10]))
    print(f"\nGate (at least {MIN_CLOSED_PER_BOOK} closed trades per book; v2 must match v1 on avg R):")
    v1 = results["v1 (analyst model)"]
    for name in ("v2 script only", "v2 script + model veto"):
        print(f"  {name}: {gate_verdict(v1, results[name])}")
    if args.replay:
        _replay(sb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
