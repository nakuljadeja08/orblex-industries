"""
allocation_context_builder.py — fetches and joins analyst signals + risk
assessment data from Supabase into a single allocation context dict.

Data flow:
    agent_reports         → analyst signals, signal strength
    risk_assessments      → market risk level, flagged tickers
    risk_ticker_details   → per-ticker risk score, max_position_pct, risk_flags
"""

import logging
from datetime import date, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# A ticker is treated as "currently chronic" if run_drift flagged a chronic flip
# (>=3 flips in 7 days) within this window. The allocator applies a mechanical
# strength haircut to these names — see run_allocator._compute_base_weights.
CHRONIC_LOOKBACK_DAYS = 7

from instrument_policy import INSTRUMENT_CLASS, INVERSE, INVERSE_MAX_HOLD_DAYS
from context_builder import _query_earnings

def _fetch_inverse_hold_days(client) -> dict[str, int]:
    """
    Consecutive most-recent allocation runs with non-zero allocation,
    per INVERSE ticker. Orders by id DESC (BIGSERIAL ≈ recency since
    allocation_details has no timestamp column).
    """
    hold = {}
    inverse_tickers = [t for t, c in INSTRUMENT_CLASS.items() if c == INVERSE]
    for tk in inverse_tickers:
        try:
            resp = (
                client.table("allocation_details")
                .select("allocated_pct")
                .eq("ticker", tk)
                .order("id", desc=True)
                .limit(INVERSE_MAX_HOLD_DAYS + 5)
                .execute()
            )
            streak = 0
            for row in (resp.data or []):
                if float(row.get("allocated_pct") or 0) > 0:
                    streak += 1
                else:
                    break
            hold[tk] = streak
        except Exception:
            hold[tk] = 0
    return hold
#Fetch helpers 
def _fetch_latest_regime(client) -> Optional[dict]:
    """Latest market regime row — degrades to None if the table is empty."""
    try:
        resp = (
            client.table("market_regimes")
            .select("regime, risk_floor, rationale, regime_date")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None
    except Exception:
        return None
    
def _fetch_latest_risk_assessment(
    client,
    risk_run_id: Optional[str] = None,
    analyst_run_id: Optional[str] = None,
) -> Optional[dict]:
    if risk_run_id:
        # Explicit risk run ID — highest priority
        resp = (
            client.table("risk_assessments")
            .select("*")
            .eq("run_id", risk_run_id)
            .single()
            .execute()
        )
        return resp.data if resp.data else None

    if analyst_run_id:
        # Look up the risk run that was generated from this specific analyst run.
        # Guard against older DB deployments where analyst_run_id column may not exist yet.
        try:
            resp = (
                client.table("risk_assessments")
                .select("*")
                .eq("analyst_run_id", analyst_run_id)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if resp.data:
                return resp.data[0]
            logger.warning(
                f"  No risk assessment found for analyst_run_id={analyst_run_id} — "
                "falling back to latest"
            )
        except Exception as e:
            if "does not exist" in str(e):
                logger.warning(
                    "  risk_assessments.analyst_run_id column missing in DB — "
                    "falling back to latest risk assessment"
                )
            else:
                raise

    # Fallback: latest by created_at
    resp = (
        client.table("risk_assessments")
        .select("*")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else None


def _fetch_risk_ticker_details(client, risk_run_id: str) -> pd.DataFrame:
    resp = (
        client.table("risk_ticker_details")
        .select("*")
        .eq("assessment_run_id", risk_run_id)
        .execute()
    )
    return pd.DataFrame(resp.data) if resp.data else pd.DataFrame()


def _fetch_chronic_flippers(client, days: int = CHRONIC_LOOKBACK_DAYS) -> set[str]:
    """
    Tickers run_drift flagged as chronic (is_chronic=True) within the last
    `days`. This is the single source of truth for "chronic" — the allocator
    reads the flag rather than recomputing flip counts. Degrades to an empty set
    (no haircut) if the table is missing or unreadable.
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    try:
        resp = (
            client.table("signal_drift")
            .select("ticker,flip_date,is_chronic")
            .eq("is_chronic", True)
            .gte("flip_date", since)
            .execute()
        )
        return {r["ticker"] for r in (resp.data or [])}
    except Exception as exc:
        logger.warning(f"  Could not fetch chronic flippers: {exc}")
        return set()


def _fetch_analyst_reports(client, analyst_run_id: str) -> pd.DataFrame:
    resp = (
        client.table("agent_reports")
        .select("ticker, signal, signal_strength, summary")
        .eq("run_id", analyst_run_id)
        .execute()
    )
    return pd.DataFrame(resp.data) if resp.data else pd.DataFrame()


# ── Main entry point ──────────────────────────────────────────────────────────

def build_allocation_context(
    client,
    risk_run_id: Optional[str] = None,
    analyst_run_id: Optional[str] = None,
    tickers: Optional[list] = None,
) -> Optional[dict]:
    """
    Returns a structured allocation context dict, or None on failure.
    analyst_run_id: pipeline run_id — used to locate the matching risk assessment
                    so the allocator always uses the same-run risk data.
    tickers: current .env tickers — filters out stale entries from old risk runs.
    """
    logger.info("  Fetching latest risk assessment...")
    risk = _fetch_latest_risk_assessment(client, risk_run_id, analyst_run_id)
    if not risk:
        logger.warning("  No risk assessments found — run run_risk.py first.")
        return None

    actual_risk_run_id   = risk["run_id"]
    analyst_run_id       = risk.get("analyst_run_id") or _infer_analyst_run_id(client)

    logger.info(f"  Risk run     : {actual_risk_run_id}")
    logger.info(f"  Analyst run  : {analyst_run_id}")

    # Fetch per-ticker risk details
    risk_details_df = _fetch_risk_ticker_details(client, actual_risk_run_id)
    if risk_details_df.empty:
        logger.warning("  No risk_ticker_details found for this run.")
        return None

    # Filter to .env tickers only
    env_set = set(tickers) if tickers else set()
    if env_set:
        dropped = set(risk_details_df["ticker"]) - env_set
        if dropped:
            logger.info(f"  Skipping {len(dropped)} ticker(s) not in .env: {sorted(dropped)}")
        risk_details_df = risk_details_df[risk_details_df["ticker"].isin(env_set)]
        if risk_details_df.empty:
            logger.warning("  No risk details remain after .env ticker filter.")
            return None

    # Fetch analyst signals (for summary context)
    analyst_df = pd.DataFrame()
    if analyst_run_id:
        analyst_df = _fetch_analyst_reports(client, analyst_run_id)

    regime = _fetch_latest_regime(client)
    if regime:
        logger.info(f"  Regime       : {regime.get('regime')}  (floor={regime.get('risk_floor') or 'none'}  date={regime.get('regime_date')})")
    else:
        logger.info("  Regime       : n/a (market_regimes table empty or unavailable)")

    chronic_flippers = _fetch_chronic_flippers(client)
    if chronic_flippers:
        logger.info(f"  Chronic flip : {sorted(chronic_flippers)} (strength haircut will apply)")

    # Merge analyst signals into risk details
    ticker_inputs = []
    for _, row in risk_details_df.iterrows():
        ticker = row["ticker"]
        analyst_row = (
            analyst_df[analyst_df["ticker"] == ticker].iloc[0].to_dict()
            if not analyst_df.empty and ticker in analyst_df["ticker"].values
            else {}
        )

        ticker_inputs.append({
            "ticker":           ticker,
            "signal":           row.get("analyst_signal") or analyst_row.get("signal", "HOLD"),
            "signal_strength":  row.get("signal_strength") or analyst_row.get("signal_strength", "WEAK"),
            "risk_score":       int(row.get("risk_score") or 5),
            "max_position_pct": float(row.get("max_position_pct") or 15.0),
            "risk_flags":       row.get("risk_flags") or [],
            "anomaly_count":    int(row.get("anomaly_count") or 0),
            "is_chronic":       ticker in chronic_flippers,
            "days_to_earnings": _query_earnings(client, ticker).get("days_to_earnings"),
        })

    return {
        "plan_date":          date.today().isoformat(),
        "analyst_run_id":     analyst_run_id or "unknown",
        "risk_run_id":        actual_risk_run_id,
        "market_risk_level":  risk.get("market_risk_level", "MODERATE"),
        "concentration_risk": risk.get("concentration_risk", "MODERATE"),
        "volatility_risk":    risk.get("volatility_risk", "MODERATE"),
        "flagged_tickers":    risk.get("flagged_tickers") or [],
        "total_tickers":      len(ticker_inputs),
        "ticker_inputs":      ticker_inputs,
        "regime":             (regime or {}).get("regime"),
        "regime_risk_floor":  (regime or {}).get("risk_floor"),
        "regime_rationale":   (regime or {}).get("rationale", ""),
        "inverse_hold_days":  _fetch_inverse_hold_days(client),
        "chronic_flippers":   sorted(chronic_flippers),
    }


def _infer_analyst_run_id(client) -> Optional[str]:
    """Fallback: get the latest analyst run_id from agent_run_log."""
    try:
        resp = (
            client.table("agent_run_log")
            .select("run_id")
            .order("run_at", desc=True)
            .limit(1)
            .execute()
        )
        if resp.data:
            return resp.data[0]["run_id"]
    except Exception:
        pass
    return None
