"""
watchdog_rules.py — the Watchdog as rules instead of a model call (EXECUTOR_DESIGN.md §3).

audit() returns exactly what watchdog_agent.audit_pipeline() returned, so
watchdog_saver and the dashboard are unchanged. The five pre-computed checks in
watchdog_context_builder were already the substance of the audit; this adds the
cross-agent issue checks the model was asked to look for, and derives status,
health score and wording from the counts using the model's own rubric:

  issues     SIGNAL_ALLOCATION_MISMATCH  BUY STRONG held at 0% though risk < 8 (MEDIUM)
             CAP_VIOLATION               allocation above max_position_pct (HIGH)
             MISSING_TICKER              a risk record pinned by ASSESSMENT_MISSING (HIGH)
             ANOMALY_IGNORED             ANOMALY_CLUSTER and > 10% allocated (MEDIUM)
             STANCE_MISMATCH             CRITICAL market risk with > 60% invested, or the
                                         report's stance breaks the report writer's own
                                         stance rules (MEDIUM)
             OTHER                       the allocator's note quotes a cash % more than
                                         2 points off the plan (MEDIUM) · the risk stage's
                                         flagged list disagrees with its scores (LOW) · a
                                         stage's run id is missing (HIGH)
  status     VIOLATIONS on any FAIL or MEDIUM/HIGH issue · WARNINGS on any WARN or
             LOW issue · else CLEAN
  health     10 − 3·FAIL − 1·WARN − 3·HIGH − 2·MEDIUM − 1·LOW, then capped at 3
             with a FAIL or HIGH and at 6 with a MEDIUM; never below 1
"""

import re
from typing import Optional

from portfolio_stance import stance

ENGINE = "rules-v1"

RECOMMEND = {
    "ALLOCATION_SUM_TO_100": "Check the allocator's renormalisation: ticker rows plus cash must total 100%.",
    "SELL_TICKERS_EXCLUDED": "Make the allocator zero every SELL before sizing.",
    "POSITION_CAPS_RESPECTED": "Re-run the enforcer clamp: an allocation exceeded its risk cap.",
    "HIGH_RISK_TICKERS_UNDERWEIGHTED": "Trim tickers scored 7+ to 15% or less.",
    "FULL_TICKER_COVERAGE": "Find why a ticker skipped the risk or allocation stage before the next run.",
    "SIGNAL_ALLOCATION_MISMATCH": "Review why a strong BUY got no allocation (cash floor, caps or drift haircut).",
    "CAP_VIOLATION": "Re-run the enforcer clamp: an allocation exceeded its risk cap.",
    "MISSING_TICKER": "Investigate the missing risk assessment; the ticker was excluded as a precaution.",
    "ANOMALY_IGNORED": "Reduce the size of anomaly-cluster names or raise their risk score.",
    "STANCE_MISMATCH": "Raise the cash floor: CRITICAL market risk with most of the book invested.",
    "OTHER": "Re-run the pipeline: a stage did not record its run id.",
}


def _num(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def expected_stance(ctx: dict) -> Optional[str]:
    """The report writer's stance rules (portfolio_stance.py), applied to this run."""
    cross = ctx.get("ticker_cross") or []
    return stance(ctx.get("market_risk_level"), ctx.get("cash_pct"), len(ctx.get("flagged_tickers") or []),
                  len(cross), sum(1 for t in cross if t.get("analyst_signal") == "BUY"))


def cash_in_note(note: Optional[str]) -> Optional[float]:
    """A cash percentage quoted in the allocator's strategy note, if there is exactly one."""
    if not note:
        return None
    # A "cash floor" is a minimum, not the cash held, so it is never read as a claim.
    hits = set(re.findall(r"(\d+(?:\.\d+)?)%\s*(?:in\s+)?cash(?!\s+floor)", note, re.I)
               + re.findall(r"cash(?: reserve| allocation| position)?\s*(?:of|at|to|is|=|:)\s*(\d+(?:\.\d+)?)%", note, re.I))
    return float(hits.pop()) if len(hits) == 1 else None


def find_issues(ctx: dict) -> list[dict]:
    issues = []

    def add(kind, sev, ticker, desc):
        issues.append({"issue_type": kind, "severity": sev, "ticker": ticker, "description": desc})

    for t in ctx.get("ticker_cross") or []:
        tk, alloc = t["ticker"], _num(t.get("allocated_pct")) or 0.0
        risk, cap = _num(t.get("risk_score")), _num(t.get("max_position_pct"))
        flags = t.get("risk_flags") or []
        sig, strength = t.get("analyst_signal"), t.get("signal_strength")
        if "ASSESSMENT_MISSING" in flags:
            add("MISSING_TICKER", "HIGH", tk, f"{tk} has no real risk assessment and was excluded as a precaution.")
        if cap is not None and alloc > cap + 0.5:
            add("CAP_VIOLATION", "HIGH", tk, f"{tk} is allocated {alloc:.1f}% above its {cap:.1f}% risk cap.")
        if sig == "BUY" and strength == "STRONG" and alloc == 0 and risk is not None and risk < 8:
            add("SIGNAL_ALLOCATION_MISMATCH", "MEDIUM", tk,
                f"{tk} is a STRONG BUY with risk {risk:.0f}/10 but received 0%.")
        if "ANOMALY_CLUSTER" in flags and alloc > 10:
            add("ANOMALY_IGNORED", "MEDIUM", tk, f"{tk} carries an anomaly cluster yet holds {alloc:.1f}%.")

    invested = _num(ctx.get("invested_pct")) or 0.0
    if ctx.get("market_risk_level") == "CRITICAL" and invested > 60:
        add("STANCE_MISMATCH", "MEDIUM", "PORTFOLIO",
            f"Market risk is CRITICAL but {invested:.1f}% of the book is invested.")

    expected = expected_stance(ctx)
    stance = ctx.get("portfolio_stance")
    if expected and stance and stance not in ("UNKNOWN", expected):
        add("STANCE_MISMATCH", "MEDIUM", "PORTFOLIO",
            f"The report says {stance}, but the report writer's own rules give {expected} "
            f"(market risk {ctx.get('market_risk_level')}, cash {ctx.get('cash_pct')}%).")

    note_cash = cash_in_note(ctx.get("alloc_strategy_note"))
    cash = _num(ctx.get("cash_pct"))
    if note_cash is not None and cash is not None and abs(note_cash - cash) > 2.0:
        add("OTHER", "MEDIUM", "PORTFOLIO",
            f"The allocator's note says {note_cash:g}% cash, but the plan holds {cash:g}% cash.")

    cross = ctx.get("ticker_cross") or []
    should_flag = {t["ticker"] for t in cross
                   if (_num(t.get("risk_score")) or 0) >= 7 or ((_num(t.get("max_position_pct")) or 99) <= 8)}
    listed = set(ctx.get("flagged_tickers") or [])
    if cross and listed != should_flag:
        diff = sorted(listed ^ should_flag)
        add("OTHER", "LOW", "PORTFOLIO",
            f"The risk stage's flagged list disagrees with its own scores for: {', '.join(diff)}.")
    for stage in ("alloc_run_id", "risk_run_id", "analyst_run_id"):
        if ctx.get(stage) in (None, "", "unknown"):
            add("OTHER", "HIGH", "PORTFOLIO", f"The report does not record its {stage.replace('_', ' ')}.")
    return issues


def audit(ctx: dict) -> dict:
    checks = ctx.get("consistency_checks") or []
    issues = find_issues(ctx)
    n = {s: sum(1 for c in checks if c["status"] == s) for s in ("PASS", "WARN", "FAIL")}
    sev = {s: sum(1 for i in issues if i["severity"] == s) for s in ("LOW", "MEDIUM", "HIGH")}

    if n["FAIL"] or sev["MEDIUM"] or sev["HIGH"]:
        status = "VIOLATIONS"
    elif n["WARN"] or sev["LOW"]:
        status = "WARNINGS"
    else:
        status = "CLEAN"
    health = 10 - 3 * n["FAIL"] - n["WARN"] - 3 * sev["HIGH"] - 2 * sev["MEDIUM"] - sev["LOW"]
    if n["FAIL"] or sev["HIGH"]:
        health = min(health, 3)
    elif sev["MEDIUM"]:
        health = min(health, 6)
    health = max(1, min(10, health))

    failed = [c["rule"] for c in checks if c["status"] != "PASS"]
    summary = (f"{n['PASS']} of {len(checks)} rule checks passed"
               + (f" ({', '.join(failed)} did not)" if failed else "")
               + f"; {len(issues)} cross-agent issue(s) found. "
               + (f"Market risk {ctx.get('market_risk_level')}, {ctx.get('active_count', 0)} active and "
                  f"{ctx.get('excluded_count', 0)} excluded tickers, {ctx.get('cash_pct', 0)}% cash."))

    cross = ctx.get("ticker_cross") or []
    buys = [t for t in cross if t.get("analyst_signal") == "BUY"]
    funded = [t for t in buys if (_num(t.get("allocated_pct")) or 0) > 0]
    sells_funded = [t["ticker"] for t in cross if t.get("analyst_signal") == "SELL" and (_num(t.get("allocated_pct")) or 0) > 0]
    quality = (f"{len(buys)} BUY, {sum(1 for t in cross if t.get('analyst_signal') == 'SELL')} SELL, "
               f"{sum(1 for t in cross if t.get('analyst_signal') == 'HOLD')} HOLD; {len(funded)} of {len(buys)} BUYs funded"
               + (f"; SELLs still funded: {', '.join(sells_funded)}" if sells_funded else "") + ".")

    recs = []
    for key in failed + [i["issue_type"] for i in issues]:
        r = RECOMMEND.get(key)
        if r and r not in recs:
            recs.append(r)

    return {
        "overall_status": status,
        "audit_summary": summary,
        "rule_check_results": [{"rule": c["rule"], "status": c["status"], "finding": c["detail"]} for c in checks],
        "agent_consistency_issues": issues,
        "signal_quality_assessment": quality,
        "recommendations": recs or ["No action needed."],
        "pipeline_health_score": health,
        "model": ENGINE, "prompt_tokens": 0, "output_tokens": 0,
    }
