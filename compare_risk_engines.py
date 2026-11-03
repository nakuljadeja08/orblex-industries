"""
compare_risk_engines.py — how closely risk_rules.py reproduces the model's past risk calls.

  history   every stored risk_ticker_details row, re-scored by the rules from the
            inputs saved alongside it (volatility, anomaly count, signal, strength).
            Earnings, options, regime and hit-rate inputs were not stored, so this
            measures the core calibration.
  today     the latest model assessment re-scored with full inputs, side by side.

Usage:
  python compare_risk_engines.py [--today]
"""

import argparse
import logging
import sys

import pandas as pd

from config import load_config
from loader import get_client
from risk_rules import score_ticker

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass
logging.disable(logging.INFO)


def history(sb) -> None:
    rows, start = [], 0
    while True:
        d = (sb.table("risk_ticker_details")
             .select("assessment_run_id,ticker,analyst_signal,signal_strength,volatility,anomaly_count,risk_score,max_position_pct")
             .order("id", desc=True).range(start, start + 999).execute().data)
        rows += d
        if len(d) < 1000:
            break
        start += 1000
    df = pd.DataFrame(rows).dropna(subset=["volatility", "risk_score"])
    out = [score_ticker({"ticker": r.ticker, "signal": r.analyst_signal, "signal_strength": r.signal_strength,
                         "volatility": float(r.volatility), "anomaly_count": int(r.anomaly_count or 0),
                         "anomalies": []}, None, set()) for r in df.itertuples()]
    df["rs"] = [o["risk_score"] for o in out]
    df["rc"] = [o["max_position_pct"] for o in out]
    df["risk_score"] = df.risk_score.astype(float)
    df["max_position_pct"] = df.max_position_pct.astype(float)

    def line(label, d):
        both = ((d.rs >= 8) & (d.risk_score >= 8)).sum()
        rules_only = ((d.rs >= 8) & (d.risk_score < 8)).sum()
        model_only = ((d.rs < 8) & (d.risk_score >= 8)).sum()
        print(f"  {label:<22} n={len(d):>4}  score ±1 {(abs(d.rs - d.risk_score) <= 1).mean():>4.0%}  "
              f"exact {(d.rs == d.risk_score).mean():>4.0%}  bias {(d.rs - d.risk_score).mean():+.2f}  "
              f"exclusion agree {((d.rs >= 8) == (d.risk_score >= 8)).mean():>4.0%} "
              f"(both {both}, rules-only {rules_only}, model-only {model_only})  "
              f"cap ±3pt {(abs(d.rc - d.max_position_pct) <= 3).mean():>4.0%}")

    print(f"History: {df.assessment_run_id.nunique()} model runs")
    line("all", df)
    line("swing era (≥ Oct 12)", df[df.assessment_run_id.str[5:13] >= "20261012"])


def today(sb) -> None:
    from performance_context import build_performance_summary
    from risk_context_builder import build_risk_context
    from risk_rules import score_risk
    a = sb.table("risk_assessments").select("*").order("created_at", desc=True).limit(1).execute().data[0]
    ctx = build_risk_context(sb, analyst_run_id=a["analyst_run_id"])
    out = score_risk(ctx, build_performance_summary(sb, tickers=ctx["tickers"]))
    llm = {d["ticker"]: d for d in sb.table("risk_ticker_details").select("*")
           .eq("assessment_run_id", a["run_id"]).execute().data}
    ti = {t["ticker"]: t for t in ctx["ticker_inputs"]}
    print(f"\nToday: analyst run {a['analyst_run_id']}")
    for k in ("market_risk_level", "concentration_risk", "volatility_risk"):
        print(f"  {k:<20} model {a[k]:<9} rules {out[k]}")
    print(f"  {'ticker':<6} {'signal':<14} {'vol':>4}   model   rules   flags")
    for d in sorted(out["ticker_details"], key=lambda d: d["ticker"]):
        m, t = llm.get(d["ticker"], {}), ti[d["ticker"]]
        print(f"  {d['ticker']:<6} {t['signal'] + ' ' + (t['signal_strength'] or ''):<14} {(t['volatility'] or 0):>4.0%}"
              f"   {m.get('risk_score')!s:>2}/{m.get('max_position_pct')!s:<5} {d['risk_score']:>2}/{d['max_position_pct']:<5} "
              f"{','.join(d['risk_flags'])}")
    ex_m = sorted(t for t, m in llm.items() if (m.get("risk_score") or 0) >= 8)
    ex_r = sorted(d["ticker"] for d in out["ticker_details"] if d["risk_score"] >= 8)
    print(f"  excluded at 8+: model {ex_m} · rules {ex_r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", action="store_true", help="also compare the latest assessment with full inputs")
    args = ap.parse_args()
    cfg = load_config()
    sb = get_client(cfg.supabase_url, cfg.supabase_key)
    history(sb)
    if args.today:
        today(sb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
