"""
risk_rules.py — the Risk Manager as rules instead of a model call (EXECUTOR_DESIGN.md §3).

score_risk() returns exactly what risk_agent.assess_risk() returned, so the saver,
allocator, trade plan, report writer and watchdog are unchanged.

The numbers are CALIBRATED to the model's own past decisions (663 risk_ticker_details
rows, 53 runs, 2026-04-28 → 2026-10-15), so switching engines keeps the same policy
while making it deterministic and free. Per ticker, risk_score 1–10 =
  volatility (annualised, 30d)  ≤25% → 3 · ≤45% → 5 · ≤55% → 7 · ≤80% → 8 · else 9
                                unknown → 5 (VOLATILITY_UNKNOWN); >60% flags HIGH_VOLATILITY
  anomalies                     none → −1 · ≥4 → +1 (≥3 flags ANOMALY_CLUSTER)
  signal                        SELL MODERATE → +1 · SELL STRONG → +2 (BEARISH_MOMENTUM)
  earnings                      ≤5 trading days → +1 EARNINGS_EVENT (event risk)
  backtest feedback             a chronic wrong-caller → +1 LOW_HIT_RATE (and cap ≤10%)
  regime                        CORRECTION +1 · BEAR +2, on every ticker
Flags without a score change (already inside the calibration): OVERBOUGHT, OVERSOLD,
BULL_TRAP_RISK, HIGH_IV, SIGNAL_CONFLICT.

max_position_pct is the model's median cap for the score (≤2 → 25 · 3 → 20 · 4 → 16 ·
5 → 14 · 6 → 10 · 7 → 8 · ≥8 → 5), SELL and LOW_HIT_RATE capped at 10, compressed
×0.75 in CORRECTION and ×0.5 in BEAR, never below 5. risk_score ≥8 is still the
allocator's exclusion line.

Portfolio levels follow the thresholds the model's prompt gave it, then only ever
escalate: the regime's risk floor, a hostile macro equity impact (≥ MODERATE) and a
paper-book drawdown of 10%+ (≥ HIGH).
"""

from collections import Counter
from typing import Optional

from instrument_policy import sector

LEVELS = ["LOW", "MODERATE", "HIGH", "CRITICAL"]
ENGINE = "rules-v2"

# 55–60% scores 8 (not 7): tightened 2026-10-15 to match the model's swing-era exclusions
# (agreement 92% → 97%, errors 0/11 one-sided → 2/2; all-history 87% → 84%).
VOL_BASE = ((0.25, 3), (0.45, 5), (0.55, 7), (0.80, 8))   # (upper bound, score); above → 9
CAPS = {1: 25.0, 2: 25.0, 3: 20.0, 4: 16.0, 5: 14.0, 6: 10.0, 7: 8.0}   # score ≥8 → 5


def _vol_base(vol: Optional[float]) -> tuple[int, list[str]]:
    if vol is None:
        return 5, ["VOLATILITY_UNKNOWN"]
    flags = ["HIGH_VOLATILITY"] if vol > 0.60 else []
    for cut, score in VOL_BASE:
        if vol <= cut:
            return score, flags
    return 9, flags


def worst_callers(perf: Optional[dict], min_n: int = 5, below: float = 0.40) -> set:
    """Tickers whose 20d (else 5d) hit rate is under `below` on at least `min_n` calls."""
    out = set()
    for t, periods in ((perf or {}).get("hit_rates") or {}).items():
        rec = periods.get(20) or periods.get(5)
        if rec and rec.get("n", 0) >= min_n and rec.get("rate", 1) < below:
            out.add(t)
    return out


def score_ticker(t: dict, regime: Optional[str], low_hit: set) -> dict:
    signal = (t.get("signal") or "HOLD").upper()
    strength = (t.get("signal_strength") or "WEAK").upper()
    score, flags = _vol_base(t.get("volatility"))
    why = [f"vol {t['volatility']:.0%}" if t.get("volatility") is not None else "vol unknown"]

    types = {str(a.get("type", "")).lower() for a in (t.get("anomalies") or [])}
    n_anom = t.get("anomaly_count") or 0
    if n_anom == 0:
        score -= 1
        why.append("no anomalies")
    elif n_anom >= 4:
        score += 1
        why.append(f"{n_anom} anomalies")
    if n_anom >= 3:
        flags.append("ANOMALY_CLUSTER")
    if "overbought" in types:
        flags.append("OVERBOUGHT")
        if signal == "BUY" and "large_price_move" in types:
            flags.append("BULL_TRAP_RISK")
    if "oversold" in types:
        flags.append("OVERSOLD")

    dte = t.get("days_to_earnings")
    if dte is not None and 0 <= dte <= 5:
        score += 1
        flags.append("EARNINGS_EVENT")
        why.append(f"earnings in {dte}d")

    if (t.get("iv_rank") or 0) >= 80:
        flags.append("HIGH_IV")
    if signal == "BUY" and (str(t.get("flow_signal") or "").upper() == "BEARISH"
                            or (t.get("put_call_ratio") or 0) >= 1.2):
        flags.append("SIGNAL_CONFLICT")

    if signal == "SELL" and strength in ("MODERATE", "STRONG"):
        score += 1 if strength == "MODERATE" else 2
        flags.append("BEARISH_MOMENTUM")
        why.append(f"SELL {strength}")
    if t["ticker"] in low_hit:
        score += 1
        flags.append("LOW_HIT_RATE")
        why.append("analyst often wrong on it")

    regime_add = {"CORRECTION": 1, "BEAR": 2}.get(regime or "", 0)
    if regime_add:
        score += regime_add
        why.append(f"{regime} regime")
    score = max(1, min(10, score))

    cap = CAPS.get(score, 5.0)
    if signal == "SELL" or "LOW_HIT_RATE" in flags:
        cap = min(cap, 10.0)
    cap = max(5.0, round(cap * {"CORRECTION": 0.75, "BEAR": 0.5}.get(regime or "", 1.0), 1))

    return {"ticker": t["ticker"], "risk_score": score, "max_position_pct": cap,
            "risk_flags": flags, "notes": "Rules: " + ", ".join(why) + f" → {score}/10, cap {cap:.0f}%."}


def _escalate(level: str, floor: Optional[str]) -> str:
    return max(level, floor, key=LEVELS.index) if floor in LEVELS else level


def score_risk(ctx: dict, perf: Optional[dict] = None) -> dict:
    """ctx: risk_context_builder.build_risk_context(); perf: performance_context summary or None."""
    mkt = ctx.get("market_context") or {}
    regime_row, macro = mkt.get("regime") or {}, mkt.get("macro") or {}
    regime = regime_row.get("regime")
    low_hit = worst_callers(perf)

    details = [score_ticker(t, regime, low_hit) for t in ctx["ticker_inputs"]]

    total = ctx.get("total_tickers") or len(details) or 1
    sc = ctx.get("signal_counts") or {}
    sell_pct = sc.get("SELL", 0) / total
    avg_vol = ctx.get("avg_volatility")
    v = avg_vol or 0.0
    if sell_pct > 0.60 or v > 0.60:
        level = "CRITICAL"
    elif sell_pct > 0.40 or v > 0.40:
        level = "HIGH"
    elif v >= 0.20 or sell_pct >= 0.20:
        level = "MODERATE"
    else:
        level = "LOW"
    reasons = [f"{sell_pct:.0%} SELL signals",
               f"average volatility {v:.0%}" if avg_vol else "average volatility unknown"]
    level = _escalate(level, regime_row.get("risk_floor"))
    if str(macro.get("equity_impact") or "").upper() == "NEGATIVE":
        level = _escalate(level, "MODERATE")
        reasons.append("macro equity impact NEGATIVE")
    dd = ((perf or {}).get("drawdown") or {}).get("current_drawdown_pct")
    if dd is not None and dd <= -10:
        level = _escalate(level, "HIGH")
        reasons.append(f"paper book {dd:.1f}% below its high")
    if regime_row.get("risk_floor"):
        reasons.append(f"regime {regime} floor {regime_row['risk_floor']}")

    buys = [t["ticker"] for t in ctx["ticker_inputs"] if (t.get("signal") or "").upper() == "BUY"]
    if len(buys) >= 3:
        top_sector, n = Counter(sector(b) for b in buys).most_common(1)[0]
        share = n / len(buys)
        concentration = "HIGH" if share > 0.5 else "MODERATE" if share > 0.3 else "LOW"
    else:
        top_sector, share, concentration = None, 0.0, "LOW"
    volatility = "HIGH" if v > 0.45 else "MODERATE" if v > 0.25 else "LOW"

    flagged = [d["ticker"] for d in details if d["risk_score"] >= 7 or d["max_position_pct"] <= 8]
    excluded = [d["ticker"] for d in details if d["risk_score"] >= 8]
    summary = (f"Market risk {level}: {'; '.join(reasons)}. "
               f"{len(flagged)} of {len(details)} tickers flagged, {len(excluded)} excluded at risk 8+.")
    reasoning = (f"Scored by rules ({ENGINE}). Concentration {concentration}"
                 + (f" ({share:.0%} of BUYs in {top_sector})" if top_sector else "")
                 + f"; volatility {volatility}. Excluded: {', '.join(excluded) or 'none'}. "
                 + f"Flagged: {', '.join(flagged) or 'none'}.")

    return {
        "market_risk_level": level, "concentration_risk": concentration, "volatility_risk": volatility,
        "risk_summary": summary, "reasoning": reasoning, "flagged_tickers": flagged,
        "ticker_details": details,
        "response_truncated": False, "missing_tickers": [], "coverage_complete": True,
        "prompt_tokens": 0, "output_tokens": 0, "model": ENGINE,
    }
