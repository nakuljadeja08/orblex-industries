"""
report_saver.py — persists agent analysis reports to Supabase
"""

import json
import logging
from datetime import date, datetime, timezone

from supabase import Client

logger = logging.getLogger(__name__)

# Added by swing_schema.sql. Written only on swing-mode rows; if the migration has
# not been applied, the save drops them and retries rather than losing the report.
SWING_COLUMNS = ("trading_mode", "setup_type", "entry_price", "stop_price", "target_price",
                 "horizon_days", "risk_reward", "invalidation", "atr_14", "swing_flags",
                 "swing_notes", "model_signal")


def save_report(
    client: Client,
    run_id: str,
    ticker: str,
    analysis: dict,
    ctx: dict = None,
) -> bool:
    """
    Upsert one agent report row into agent_reports.
    Returns True on success.
    """
    try:
        record = {
            "run_id":          run_id,
            "ticker":          ticker,
            "report_date":     date.today().isoformat(),
            "signal":          analysis["signal"],
            "signal_strength": analysis["signal_strength"],
            "summary":         analysis["summary"],
            "anomalies":       json.dumps(analysis.get("anomalies", [])),
            "reasoning":       analysis["reasoning"],
            "model":           analysis.get("model"),
            "prompt_tokens":   analysis.get("prompt_tokens"),
            "output_tokens":   analysis.get("output_tokens"),
            "news_used":       analysis.get("news_used", False),
            "sentiment_score": analysis.get("sentiment_score"),
            "valuation_view":  analysis.get("valuation_view", "N/A"),
            "news_count":      len((ctx or {}).get("news") or []),
        }

        if (ctx or {}).get("trading_mode") == "swing":
            record.update({
                "trading_mode":  "swing",
                "setup_type":    analysis.get("setup_type"),
                "entry_price":   analysis.get("entry_price"),
                "stop_price":    analysis.get("stop_price"),
                "target_price":  analysis.get("target_price"),
                "horizon_days":  analysis.get("horizon_days"),
                "risk_reward":   analysis.get("risk_reward"),
                "invalidation":  analysis.get("invalidation"),
                "atr_14":        (ctx.get("swing") or {}).get("atr_14"),
                "swing_flags":   analysis.get("swing_flags") or [],
                "swing_notes":   analysis.get("swing_notes") or [],
                "model_signal":  analysis.get("model_signal", analysis["signal"]),
            })

        try:
            client.table("agent_reports").upsert(record, on_conflict="run_id,ticker").execute()
        except Exception as e:
            msg = str(e)
            if not any(c in msg for c in SWING_COLUMNS) or not ("PGRST204" in msg or "schema cache" in msg):
                raise
            logger.warning(
                "  agent_reports: swing columns not found — saving without the trade "
                "plan. Apply swing_schema.sql in Supabase to keep entry/stop/target."
            )
            for c in SWING_COLUMNS:
                record.pop(c, None)
            client.table("agent_reports").upsert(record, on_conflict="run_id,ticker").execute()

        logger.info(
            f"  [OK] Report saved - {ticker}  "
            f"signal={analysis['signal']} ({analysis['signal_strength']})  "
            f"news={'yes' if record['news_used'] else 'no'}"
        )
        return True

    except Exception as exc:
        logger.error(f"  [FAIL] Failed to save report for {ticker}: {exc}")
        return False


def save_run_log(
    client: Client,
    run_id: str,
    tickers: list[str],
    reports_written: int,
    status: str,
    error_message: str = None,
) -> None:
    """Write a summary row to agent_run_log."""
    try:
        client.table("agent_run_log").insert({
            "run_id":          run_id,
            "run_at":          datetime.now(timezone.utc).isoformat(),
            "tickers":         tickers,
            "reports_written": reports_written,
            "status":          status,
            "error_message":   error_message,
        }).execute()
    except Exception as exc:
        logger.warning(f"Could not write agent run log: {exc}")