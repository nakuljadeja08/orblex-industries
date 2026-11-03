"""
risk_saver.py — persists Risk Manager Agent output to Supabase.

Tables written:
  - risk_assessments       (one row per run)
  - risk_ticker_details    (one row per ticker per run)
"""

import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)


def save_risk_assessment(
    client,
    run_id: str,
    ctx: dict,
    assessment: dict,
    analyst_run_id: str = None,
    dry_run: bool = False,
) -> bool:
    """
    Upserts risk assessment data to Supabase.
    Returns True on success, False on failure.
    """
    sc = ctx["signal_counts"]

    # ── Build position_limits map from ticker_details ─────────────────────────
    position_limits = {}
    for td in assessment.get("ticker_details", []):
        position_limits[td["ticker"]] = td.get("max_position_pct", 15.0)

    # ── Compute portfolio-level overall_risk_score from per-ticker scores ─────
    ticker_details = assessment.get("ticker_details", [])
    if ticker_details:
        scores = [td.get("risk_score") for td in ticker_details if td.get("risk_score") is not None]
        overall_risk_score = round(sum(scores) / len(scores), 1) if scores else None
    else:
        overall_risk_score = None

    # ── risk_assessments row ──────────────────────────────────────────────────
    assessment_row = {
        "run_id":              run_id,
        "analyst_run_id":      analyst_run_id or ctx.get("source_run_id"),
        "assessment_date":     ctx["assessment_date"],
        "total_tickers":       ctx["total_tickers"],
        "buy_count":           sc.get("BUY", 0),
        "sell_count":          sc.get("SELL", 0),
        "hold_count":          sc.get("HOLD", 0),
        "overall_risk_score":  overall_risk_score,
        "market_risk_level":   assessment.get("market_risk_level", "MODERATE"),
        "concentration_risk":  assessment.get("concentration_risk", "MODERATE"),
        "volatility_risk":     assessment.get("volatility_risk", "MODERATE"),
        "position_limits":     position_limits,
        "flagged_tickers":     assessment.get("flagged_tickers", []),
        "risk_summary":        assessment.get("risk_summary"),
        "reasoning":           assessment.get("reasoning"),
        "model":               assessment.get("model"),
        "prompt_tokens":       assessment.get("prompt_tokens"),
        "output_tokens":       assessment.get("output_tokens"),
    }

    # ── risk_ticker_details rows ───────────────────────────────────────────────
    detail_rows = []
    ticker_map = {t["ticker"]: t for t in ctx["ticker_inputs"]}

    for td in assessment.get("ticker_details", []):
        ticker = td["ticker"]
        ticker_ctx = ticker_map.get(ticker, {})

        detail_rows.append({
            "assessment_run_id": run_id,
            "ticker":            ticker,
            "analyst_signal":    ticker_ctx.get("signal"),
            "signal_strength":   ticker_ctx.get("signal_strength"),
            "volatility":        ticker_ctx.get("volatility"),
            "anomaly_count":     ticker_ctx.get("anomaly_count", 0),
            "risk_score":        td.get("risk_score"),
            "max_position_pct":  td.get("max_position_pct"),
            "risk_flags":        td.get("risk_flags", []),
            "notes":             td.get("notes"),
        })

    if dry_run:
        logger.info("  [DRY RUN] Would write to risk_assessments:")
        logger.info(f"    market_risk_level  : {assessment_row['market_risk_level']}")
        logger.info(f"    flagged_tickers    : {assessment_row['flagged_tickers']}")
        logger.info(f"    position_limits    : {position_limits}")
        logger.info(f"  [DRY RUN] Would write {len(detail_rows)} rows to risk_ticker_details")
        return True

    try:
        # Upsert assessment (on conflict: run_id)
        client.table("risk_assessments").upsert(
            assessment_row, on_conflict="run_id"
        ).execute()
        logger.info("  Saved risk_assessments row.")

        # Upsert ticker details
        if detail_rows:
            client.table("risk_ticker_details").upsert(
                detail_rows, on_conflict="assessment_run_id,ticker"
            ).execute()
            logger.info(f"  Saved {len(detail_rows)} risk_ticker_details rows.")

        return True

    except Exception as e:
        logger.error(f"  Supabase write error: {e}")
        return False
