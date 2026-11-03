"""
risk_context_builder.py — reads agent_reports and stock_prices from Supabase,
then builds a structured risk context for the Risk Manager Agent.

Input  : latest agent_reports rows (one per ticker from the latest run)
Output : a dict containing portfolio-level and per-ticker risk inputs
"""

import logging
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from context_builder import _query_earnings, _query_options_flow, build_market_context

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fetch_latest_reports(client, run_id: Optional[str] = None) -> pd.DataFrame:
    """
    Fetch analyst reports. If run_id is given, fetch that run.
    Otherwise fetch the most recent report for each ticker.
    """
    if run_id:
        resp = (
            client.table("agent_reports")
            .select("*")
            .eq("run_id", run_id)
            .execute()
        )
    else:
        # Get the latest run_id
        resp = (
            client.table("agent_reports")
            .select("run_id")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not resp.data:
            return pd.DataFrame()
        latest_run_id = resp.data[0]["run_id"]
        resp = (
            client.table("agent_reports")
            .select("*")
            .eq("run_id", latest_run_id)
            .execute()
        )

    return pd.DataFrame(resp.data) if resp.data else pd.DataFrame()


def _fetch_volatility(client, ticker: str, days: int = 30) -> Optional[float]:
    """Compute annualised volatility from recent stock_prices."""
    since = (date.today() - timedelta(days=days)).isoformat()
    resp = (
        client.table("stock_prices")
        .select("date, close")
        .eq("ticker", ticker)
        .gte("date", since)
        .order("date", desc=False)
        .execute()
    )
    if not resp.data or len(resp.data) < 5:
        return None

    df = pd.DataFrame(resp.data)
    df["close"] = df["close"].astype(float)
    daily_returns = df["close"].pct_change().dropna()
    vol = float(daily_returns.std() * (252 ** 0.5))
    return round(vol, 4)


# ── Signal distribution helpers ───────────────────────────────────────────────

def _signal_counts(reports_df: pd.DataFrame) -> dict:
    counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for sig in reports_df["signal"].dropna():
        if sig.upper() in counts:
            counts[sig.upper()] += 1
    return counts


def _strength_weight(strength: str) -> float:
    return {"STRONG": 1.0, "MODERATE": 0.6, "WEAK": 0.3}.get(
        str(strength).upper(), 0.3
    )


# ── Per-ticker risk inputs ────────────────────────────────────────────────────

def _build_ticker_inputs(client, row: dict) -> dict:
    ticker = row["ticker"]
    anomalies = row.get("anomalies") or []
    if isinstance(anomalies, str):
        import json
        try:
            anomalies = json.loads(anomalies)
        except Exception:
            anomalies = []

    # Pull RSI from the indicators embedded in reasoning (best effort)
    # Volatility is computed fresh from stock_prices
    vol = _fetch_volatility(client, ticker)

    # Stage 0 risk inputs: imminent earnings is binary event risk, and options
    # positioning (put/call, IV rank, flow) is forward-looking risk the analyst's
    # backward-looking technicals do not capture.
    earn = _query_earnings(client, ticker)
    opt  = _query_options_flow(client, ticker)

    return {
        "ticker":           ticker,
        "signal":           row.get("signal", "HOLD"),
        "signal_strength":  row.get("signal_strength", "WEAK"),
        "strength_weight":  _strength_weight(row.get("signal_strength", "WEAK")),
        "anomaly_count":    len(anomalies),
        "anomalies":        anomalies,
        "volatility":       vol,
        "days_to_earnings": earn.get("days_to_earnings"),
        "earnings_risk":    earn.get("earnings_risk"),
        "put_call_ratio":   opt.get("put_call_ratio_volume"),
        "iv_rank":          opt.get("iv_rank"),
        "flow_signal":      opt.get("flow_signal"),
        "summary":          row.get("summary", ""),
        "reasoning":        row.get("reasoning", ""),
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def build_risk_context(client, analyst_run_id: Optional[str] = None) -> Optional[dict]:
    """
    Returns a structured risk context dict ready for the Risk Manager Agent,
    or None if there are no analyst reports to work from.
    """
    logger.info("  Fetching analyst reports from Supabase...")
    reports_df = _fetch_latest_reports(client, analyst_run_id)

    if reports_df.empty:
        logger.warning("  No analyst reports found — run run_agent.py first.")
        return None

    source_run_id = reports_df["run_id"].iloc[0]
    assessment_date = date.today().isoformat()
    tickers = sorted(reports_df["ticker"].tolist())

    logger.info(f"  Found {len(tickers)} tickers from analyst run {source_run_id}")

    # Signal distribution
    signal_counts = _signal_counts(reports_df)

    # Per-ticker enriched inputs
    ticker_inputs = []
    for _, row in reports_df.iterrows():
        logger.info(f"    Enriching {row['ticker']}...")
        ticker_inputs.append(_build_ticker_inputs(client, row.to_dict()))

    # Portfolio-level stats
    avg_volatility = None
    vols = [t["volatility"] for t in ticker_inputs if t["volatility"] is not None]
    if vols:
        avg_volatility = round(sum(vols) / len(vols), 4)

    total_anomalies = sum(t["anomaly_count"] for t in ticker_inputs)

    return {
        "assessment_date":   assessment_date,
        "source_run_id":     source_run_id,
        "tickers":           tickers,
        "total_tickers":     len(tickers),
        "signal_counts":     signal_counts,
        "avg_volatility":    avg_volatility,
        "total_anomalies":   total_anomalies,
        "ticker_inputs":     ticker_inputs,
        "market_context":    build_market_context(client),
    }
