"""
report_writer_context_builder.py — assembles the full pipeline context
for the Report Writer Agent.

Pulls the latest completed run from all four upstream stages:
  - agent_reports          (Analyst Agent)
  - risk_assessments       (Risk Manager)
  - risk_ticker_details    (Risk Manager per-ticker)
  - allocation_plans       (Allocator)
  - allocation_details     (Allocator per-ticker)
"""

import logging
from datetime import date
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ── Fetch helpers ─────────────────────────────────────────────────────────────

def _fetch_latest_allocation(client, alloc_run_id: Optional[str] = None) -> Optional[dict]:
    if alloc_run_id:
        resp = (
            client.table("allocation_plans")
            .select("*")
            .eq("run_id", alloc_run_id)
            .single()
            .execute()
        )
        return resp.data if resp.data else None
    else:
        resp = (
            client.table("allocation_plans")
            .select("*")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None


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


# ── Main entry point ──────────────────────────────────────────────────────────

def build_report_context(
    client,
    alloc_run_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Returns a fully assembled report context dict, or None on failure.
    """
    logger.info("  Fetching latest allocation plan...")
    alloc = _fetch_latest_allocation(client, alloc_run_id)
    if not alloc:
        logger.warning("  No allocation plans found — run run_allocator.py first.")
        return None

    actual_alloc_run_id = alloc["run_id"]
    risk_run_id         = alloc.get("risk_run_id")
    analyst_run_id      = alloc.get("analyst_run_id")

    logger.info(f"  Allocation run : {actual_alloc_run_id}")
    logger.info(f"  Risk run       : {risk_run_id}")
    logger.info(f"  Analyst run    : {analyst_run_id}")

    # Fetch downstream data
    alloc_details_df = _fetch_allocation_details(client, actual_alloc_run_id)
    risk             = _fetch_risk_assessment(client, risk_run_id) if risk_run_id else {}
    risk_details_df  = _fetch_risk_ticker_details(client, risk_run_id) if risk_run_id else pd.DataFrame()
    analyst_df       = _fetch_analyst_reports(client, analyst_run_id) if analyst_run_id else pd.DataFrame()

    # Build per-ticker summary combining all layers
    ticker_summaries = []
    tickers = alloc_details_df["ticker"].tolist() if not alloc_details_df.empty else []

    for ticker in tickers:
        alloc_row  = alloc_details_df[alloc_details_df["ticker"] == ticker].iloc[0].to_dict() if not alloc_details_df.empty else {}
        risk_row   = risk_details_df[risk_details_df["ticker"] == ticker].iloc[0].to_dict() if not risk_details_df.empty and ticker in risk_details_df["ticker"].values else {}
        analyst_row= analyst_df[analyst_df["ticker"] == ticker].iloc[0].to_dict() if not analyst_df.empty and ticker in analyst_df["ticker"].values else {}

        ticker_summaries.append({
            "ticker":              ticker,
            "signal":              alloc_row.get("analyst_signal") or analyst_row.get("signal", "HOLD"),
            "signal_strength":     alloc_row.get("signal_strength") or analyst_row.get("signal_strength", "WEAK"),
            "analyst_summary":     analyst_row.get("summary", ""),
            "anomalies":           analyst_row.get("anomalies", []),
            "risk_score":          alloc_row.get("risk_score") or risk_row.get("risk_score", 5),
            "risk_flags":          alloc_row.get("risk_flags") or risk_row.get("risk_flags", []),
            "max_position_pct":    alloc_row.get("max_position_pct") or risk_row.get("max_position_pct", 15.0),
            "allocated_pct":       alloc_row.get("allocated_pct", 0.0),
            "sizing_method":       alloc_row.get("sizing_method", "SIGNAL_WEIGHTED"),
            "allocation_rationale":alloc_row.get("allocation_rationale", ""),
            "volatility":          risk_row.get("volatility"),
            "notes":               risk_row.get("notes", ""),
        })

    return {
        "report_date":        date.today().isoformat(),
        "alloc_run_id":       actual_alloc_run_id,
        "risk_run_id":        risk_run_id or "unknown",
        "analyst_run_id":     analyst_run_id or "unknown",
        # Portfolio-level
        "market_risk_level":  risk.get("market_risk_level", "MODERATE") if risk else "MODERATE",
        "concentration_risk": risk.get("concentration_risk", "MODERATE") if risk else "MODERATE",
        "volatility_risk":    risk.get("volatility_risk", "MODERATE") if risk else "MODERATE",
        "risk_summary":       risk.get("risk_summary", "") if risk else "",
        "flagged_tickers":    risk.get("flagged_tickers", []) if risk else [],
        "invested_pct":       alloc.get("invested_pct", 0.0),
        "cash_pct":           alloc.get("cash_pct", 0.0),
        "strategy_note":      alloc.get("strategy_note", ""),
        "total_tickers":      len(ticker_summaries),
        # Per-ticker
        "ticker_summaries":   ticker_summaries,
    }
