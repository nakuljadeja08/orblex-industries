"""
report_writer_saver.py — persists Report Writer Agent output to Supabase.

Tables written:
  - final_reports          (one row per run, full report JSON)
  - final_report_tickers   (one row per ticker per run, narrative)
"""

import logging

logger = logging.getLogger(__name__)


def save_report(
    client,
    run_id: str,
    ctx: dict,
    report: dict,
    dry_run: bool = False,
) -> bool:
    """
    Upserts the final report and per-ticker narratives to Supabase.
    Returns True on success, False on failure.
    """
    report_row = {
        "run_id":               run_id,
        "report_date":          ctx["report_date"],
        "alloc_run_id":         ctx["alloc_run_id"],
        "risk_run_id":          ctx["risk_run_id"],
        "analyst_run_id":       ctx["analyst_run_id"],
        "market_risk_level":    ctx["market_risk_level"],
        "invested_pct":         ctx["invested_pct"],
        "cash_pct":             ctx["cash_pct"],
        "portfolio_stance":     report.get("portfolio_stance"),
        "confidence_level":     report.get("confidence_level"),
        "confidence_reasoning": report.get("confidence_reasoning"),
        "executive_summary":    report.get("executive_summary"),
        "market_outlook":       report.get("market_outlook"),
        "key_themes":           report.get("key_themes", []),
        "risk_highlights":      report.get("risk_highlights"),
        "anomaly_digest":       report.get("anomaly_digest"),
        "watchdog_notes":       report.get("watchdog_notes"),
        "disclaimer":           report.get("disclaimer"),
        "model":                report.get("model"),
        "prompt_tokens":        report.get("prompt_tokens"),
        "output_tokens":        report.get("output_tokens"),
    }

    # Per-ticker narrative rows
    ticker_rows = []
    for narrative in report.get("ticker_narratives", []):
        ticker_rows.append({
            "report_run_id":  run_id,
            "ticker":         narrative.get("ticker"),
            "one_liner":      narrative.get("one_liner"),
            "full_narrative": narrative.get("full_narrative"),
        })

    if dry_run:
        logger.info("  [DRY RUN] final_reports row:")
        logger.info(f"    portfolio_stance   : {report_row['portfolio_stance']}")
        logger.info(f"    confidence_level   : {report_row['confidence_level']}")
        logger.info(f"    executive_summary  : {report_row['executive_summary'][:120] if report_row['executive_summary'] else 'n/a'}...")
        logger.info(f"    watchdog_notes     : {report_row['watchdog_notes']}")
        logger.info(f"  [DRY RUN] {len(ticker_rows)} ticker narrative rows would be written.")
        return True

    try:
        client.table("final_reports").upsert(
            report_row, on_conflict="run_id"
        ).execute()
        logger.info("  Saved final_reports row.")

        if ticker_rows:
            client.table("final_report_tickers").upsert(
                ticker_rows, on_conflict="report_run_id,ticker"
            ).execute()
            logger.info(f"  Saved {len(ticker_rows)} final_report_tickers rows.")

        return True

    except Exception as e:
        logger.error(f"  Supabase write error: {e}")
        return False
