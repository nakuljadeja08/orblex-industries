"""
watchdog_context_builder.py — assembles the full pipeline run for the Watchdog Agent.

The Watchdog receives the complete picture of one pipeline cycle and checks
for internal inconsistencies, logic violations, and agent failures.

Data sources:
  - agent_reports        (Analyst)
  - risk_assessments     (Risk Manager)
  - risk_ticker_details  (Risk Manager per-ticker)
  - allocation_plans     (Allocator)
  - allocation_details   (Allocator per-ticker)
  - final_reports        (Report Writer)
"""

import logging
from datetime import date
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


def _fetch_latest_report(client, report_run_id: Optional[str] = None) -> Optional[dict]:
    if report_run_id:
        resp = (
            client.table("final_reports")
            .select("*")
            .eq("run_id", report_run_id)
            .single()
            .execute()
        )
        return resp.data if resp.data else None
    else:
        resp = (
            client.table("final_reports")
            .select("*")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None


def _fetch_allocation_plan(client, alloc_run_id: str) -> Optional[dict]:
    resp = (
        client.table("allocation_plans")
        .select("*")
        .eq("run_id", alloc_run_id)
        .single()
        .execute()
    )
    return resp.data if resp.data else None


def _fetch_allocation_details(client, alloc_run_id: str) -> pd.DataFrame:
    resp = (
        client.table("allocation_details")
        .select("*")
        .eq("plan_run_id", alloc_run_id)
        .execute()
    )
    return pd.DataFrame(resp.data) if resp.data else pd.DataFrame()


def _fetch_risk_assessment(client, risk_run_id: str) -> Optional[dict]:
    resp = (
        client.table("risk_assessments")
        .select("*")
        .eq("run_id", risk_run_id)
        .single()
        .execute()
    )
    return resp.data if resp.data else None


def _fetch_risk_ticker_details(client, risk_run_id: str) -> pd.DataFrame:
    resp = (
        client.table("risk_ticker_details")
        .select("*")
        .eq("assessment_run_id", risk_run_id)
        .execute()
    )
    return pd.DataFrame(resp.data) if resp.data else pd.DataFrame()


def _fetch_analyst_reports(client, analyst_run_id: str) -> pd.DataFrame:
    resp = (
        client.table("agent_reports")
        .select("*")
        .eq("run_id", analyst_run_id)
        .execute()
    )
    return pd.DataFrame(resp.data) if resp.data else pd.DataFrame()


def _run_consistency_checks(alloc, alloc_df, risk, risk_df, analyst_df) -> list:
    """
    Rule-based consistency checks run before the LLM.
    Returns a list of {rule, status, detail} dicts.
    """
    checks = []

    # 1. Allocation sums to 100
    # ── FIX: use sum(alloc_df.allocated_pct) + cash_pct, NOT invested_pct + cash_pct
    # invested_pct only counts non-excluded (>0%) tickers, so it will never sum
    # to 100% when any ticker is excluded. The correct invariant is:
    #   sum(ALL ticker rows including 0% excluded) + cash_pct = 100%
    if alloc and not alloc_df.empty:
        ticker_sum = round(float(alloc_df["allocated_pct"].sum()), 2)
        cash_pct   = round(float(alloc.get("cash_pct") or 0.0), 2)
        total      = round(ticker_sum + cash_pct, 2)
        excluded_n = int((alloc_df["allocated_pct"] == 0).sum())
        active_n   = int((alloc_df["allocated_pct"] > 0).sum())
        checks.append({
            "rule":   "ALLOCATION_SUM_TO_100",
            "status": "PASS" if abs(total - 100.0) < 1.0 else "FAIL",
            "detail": (
                f"Sum of all ticker allocations ({ticker_sum}%) + cash ({cash_pct}%) = {total}%. "
                f"{active_n} active ticker(s), {excluded_n} excluded at 0% by design."
            ),
        })

    # 2. SELL signals should have 0% allocation
    if not alloc_df.empty and not analyst_df.empty:
        sell_tickers = analyst_df[analyst_df["signal"] == "SELL"]["ticker"].tolist()
        violations   = []
        for row in alloc_df.itertuples():
            if row.ticker in sell_tickers and row.allocated_pct > 0:
                violations.append(f"{row.ticker}={row.allocated_pct}%")
        checks.append({
            "rule":   "SELL_TICKERS_EXCLUDED",
            "status": "PASS" if not violations else "FAIL",
            "detail": f"SELL tickers with non-zero allocation: {violations or 'none'}",
        })

    # 3. No allocation exceeds max_position_pct
    if not alloc_df.empty and not risk_df.empty:
        violations = []
        for row in alloc_df.itertuples():
            r = risk_df[risk_df["ticker"] == row.ticker]
            if not r.empty:
                cap = float(r.iloc[0].get("max_position_pct") or 100.0)
                if row.allocated_pct > cap + 0.5:
                    violations.append(f"{row.ticker}: {row.allocated_pct}% > cap {cap}%")
        checks.append({
            "rule":   "POSITION_CAPS_RESPECTED",
            "status": "PASS" if not violations else "FAIL",
            "detail": f"Cap violations: {violations or 'none'}",
        })

    # 4. Flagged tickers (risk_score >= 7) should be low allocation
    if not alloc_df.empty and not risk_df.empty:
        warnings = []
        for row in risk_df.itertuples():
            rs = getattr(row, "risk_score", 0) or 0
            if rs >= 7:
                a = alloc_df[alloc_df["ticker"] == row.ticker]
                if not a.empty and float(a.iloc[0]["allocated_pct"]) > 15.0:
                    warnings.append(f"{row.ticker}: risk_score={rs}, allocated={a.iloc[0]['allocated_pct']}%")
        checks.append({
            "rule":   "HIGH_RISK_TICKERS_UNDERWEIGHTED",
            "status": "WARN" if warnings else "PASS",
            "detail": f"High-risk tickers with >15% allocation: {warnings or 'none'}",
        })

    # 5. Ticker coverage — all analyst tickers covered by risk and allocation
    if not analyst_df.empty:
        analyst_tickers = set(analyst_df["ticker"].tolist())
        risk_tickers    = set(risk_df["ticker"].tolist())  if not risk_df.empty   else set()
        alloc_tickers   = set(alloc_df["ticker"].tolist()) if not alloc_df.empty  else set()
        missing_risk    = analyst_tickers - risk_tickers
        missing_alloc   = analyst_tickers - alloc_tickers
        checks.append({
            "rule":   "FULL_TICKER_COVERAGE",
            "status": "PASS" if not missing_risk and not missing_alloc else "FAIL",
            "detail": f"Missing in risk: {list(missing_risk) or 'none'}  |  Missing in alloc: {list(missing_alloc) or 'none'}",
        })

    return checks


def build_watchdog_context(
    client,
    report_run_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Returns the full pipeline context for the Watchdog Agent, or None on failure.
    """
    logger.info("  Fetching latest final report...")
    report = _fetch_latest_report(client, report_run_id)
    if not report:
        logger.warning("  No final reports found — run run_report_writer.py first.")
        return None

    alloc_run_id   = report.get("alloc_run_id")
    risk_run_id    = report.get("risk_run_id")
    analyst_run_id = report.get("analyst_run_id")

    logger.info(f"  Report run   : {report['run_id']}")
    logger.info(f"  Alloc run    : {alloc_run_id}")
    logger.info(f"  Risk run     : {risk_run_id}")
    logger.info(f"  Analyst run  : {analyst_run_id}")

    # Fetch all pipeline data
    alloc      = _fetch_allocation_plan(client, alloc_run_id)    if alloc_run_id    else {}
    alloc_df   = _fetch_allocation_details(client, alloc_run_id) if alloc_run_id    else pd.DataFrame()
    risk       = _fetch_risk_assessment(client, risk_run_id)     if risk_run_id     else {}
    risk_df    = _fetch_risk_ticker_details(client, risk_run_id) if risk_run_id     else pd.DataFrame()
    analyst_df = _fetch_analyst_reports(client, analyst_run_id)  if analyst_run_id  else pd.DataFrame()

    # Pre-compute consistency checks
    checks = _run_consistency_checks(alloc, alloc_df, risk, risk_df, analyst_df)

    # ── Compute allocation accounting fields for prompt context ───────────────
    if not alloc_df.empty:
        ticker_sum_pct   = round(float(alloc_df["allocated_pct"].sum()), 2)
        excluded_count   = int((alloc_df["allocated_pct"] == 0).sum())
        active_count     = int((alloc_df["allocated_pct"] > 0).sum())
        cash_pct_val     = round(float(alloc.get("cash_pct") or 0.0), 2)
        allocation_total = round(ticker_sum_pct + cash_pct_val, 2)
    else:
        ticker_sum_pct   = 0.0
        excluded_count   = 0
        active_count     = 0
        cash_pct_val     = round(float(alloc.get("cash_pct") or 0.0), 2) if alloc else 0.0
        allocation_total = cash_pct_val

    # Build per-ticker cross-agent view
    tickers = alloc_df["ticker"].tolist() if not alloc_df.empty else []
    ticker_cross = []
    for ticker in tickers:
        a_row  = alloc_df[alloc_df["ticker"] == ticker].iloc[0].to_dict() if not alloc_df.empty and ticker in alloc_df["ticker"].values else {}
        r_row  = risk_df[risk_df["ticker"] == ticker].iloc[0].to_dict()   if not risk_df.empty  and ticker in risk_df["ticker"].values  else {}
        an_row = analyst_df[analyst_df["ticker"] == ticker].iloc[0].to_dict() if not analyst_df.empty and ticker in analyst_df["ticker"].values else {}

        ticker_cross.append({
            "ticker":           ticker,
            "analyst_signal":   an_row.get("signal", "UNKNOWN"),
            "signal_strength":  an_row.get("signal_strength", "UNKNOWN"),
            "risk_score":       r_row.get("risk_score", "N/A"),
            "max_position_pct": r_row.get("max_position_pct", "N/A"),
            "risk_flags":       r_row.get("risk_flags", []),
            "allocated_pct":    a_row.get("allocated_pct", 0.0),
            "sizing_method":    a_row.get("sizing_method", "UNKNOWN"),
        })

    return {
        "watchdog_date":         date.today().isoformat(),
        "report_run_id":         report["run_id"],
        "alloc_run_id":          alloc_run_id or "unknown",
        "risk_run_id":           risk_run_id or "unknown",
        "analyst_run_id":        analyst_run_id or "unknown",
        # Portfolio-level summaries
        "market_risk_level":     risk.get("market_risk_level", "UNKNOWN") if risk else "UNKNOWN",
        "portfolio_stance":      report.get("portfolio_stance", "UNKNOWN"),
        "confidence_level":      report.get("confidence_level", "UNKNOWN"),
        "invested_pct":          alloc.get("invested_pct", 0.0) if alloc else 0.0,
        "cash_pct":              cash_pct_val,
        "flagged_tickers":       risk.get("flagged_tickers", []) if risk else [],
        "report_watchdog_notes": report.get("watchdog_notes", ""),
        "alloc_strategy_note":   alloc.get("strategy_note", "") if alloc else "",
        # ── NEW: allocation accounting context ────────────────────────────────
        "ticker_sum_pct":        ticker_sum_pct,    # sum of ALL ticker rows incl. 0%
        "excluded_count":        excluded_count,    # tickers at 0% by design
        "active_count":          active_count,      # tickers with >0% allocation
        "allocation_total":      allocation_total,  # ticker_sum + cash (should = 100%)
        # Per-ticker cross-agent view
        "ticker_cross":          ticker_cross,
        # Pre-computed checks
        "consistency_checks":    checks,
    }