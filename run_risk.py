"""
run_risk.py — Orblex Industries Risk Manager Agent entry point.

Usage:
    python run_risk.py                                  # assess latest analyst run
    python run_risk.py --analyst-run-id <run_id>        # explicit analyst run
    python run_risk.py --dry-run                        # print output, skip save
"""

import argparse
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

from config import load_config
from loader import get_client
from risk_context_builder import build_risk_context
from risk_agent import assess_risk
from risk_rules import score_risk
from performance_context import build_performance_summary, format_risk_feedback
from risk_saver import save_risk_assessment

# Model/report text contains non-cp1252 characters (≤, —). Without this the
# Windows console encoder raises UnicodeEncodeError inside the log handler.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("risk_agent.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="Orblex Industries Risk Manager Agent")
    parser.add_argument(
        "--analyst-run-id",
        help="Analyst run_id to assess (default: latest run in Supabase)",
        default=None,
    )
    # Legacy alias — accepted for backwards compatibility
    parser.add_argument("--run-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def run():
    args   = parse_args()
    run_id = f"risk-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    # --analyst-run-id takes priority; --run-id is legacy fallback
    analyst_run_id = args.analyst_run_id or args.run_id

    try:
        cfg = load_config()
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)

    logger.info("=" * 60)
    logger.info(f"Orblex Industries — Risk Manager Agent")
    logger.info(f"Run ID        : {run_id}")
    logger.info(f"Analyst run   : {analyst_run_id or 'latest'}")
    logger.info(f"Dry run       : {args.dry_run}")
    logger.info("=" * 60)

    client = get_client(cfg.supabase_url, cfg.supabase_key)

    logger.info("\n[1/3] Building risk context from analyst reports...")
    ctx = build_risk_context(client, analyst_run_id=analyst_run_id)
    if ctx is None:
        logger.error("  No analyst reports found. Run run_agent.py first.")
        sys.exit(1)

    logger.info(
        f"  Tickers        : {', '.join(ctx['tickers'])}\n"
        f"  Signal counts  : BUY={ctx['signal_counts'].get('BUY',0)}"
        f"  SELL={ctx['signal_counts'].get('SELL',0)}"
        f"  HOLD={ctx['signal_counts'].get('HOLD',0)}\n"
        f"  Avg volatility : {ctx['avg_volatility']:.2%}" if ctx['avg_volatility'] else
        f"  Avg volatility : N/A"
    )
    logger.info(f"  Total anomalies: {ctx['total_anomalies']}")

    # Feedback loop (Stage 9 backtest): portfolio alpha, drawdown, and the names
    # the analyst has been most wrong about. None on a system with no backtest yet.
    perf_summary = build_performance_summary(client, tickers=ctx["tickers"])
    ctx["performance_feedback"] = format_risk_feedback(perf_summary)
    logger.info(
        f"  Backtest feedback: {'loaded (run ' + perf_summary['backtest_run_id'] + ')' if perf_summary else 'unavailable'}"
    )

    # RISK_ENGINE=rules (default): risk_rules.py, calibrated to the model's past calls,
    # deterministic and free. RISK_ENGINE=llm rolls back to the Claude Risk Manager.
    engine = os.getenv("RISK_ENGINE", "rules").strip().lower()
    if engine == "llm":
        logger.info("\n[2/3] Running Claude Risk Manager Agent...")
        assessment = assess_risk(ctx)
    else:
        logger.info("\n[2/3] Scoring risk with rules (risk_rules.py)...")
        assessment = score_risk(ctx, perf_summary)
    if assessment is None:
        logger.error("  Risk assessment failed.")
        sys.exit(1)

    logger.info(f"\n  -- RISK ASSESSMENT RESULTS --")
    logger.info(f"  Market Risk Level  : {assessment.get('market_risk_level')}")
    logger.info(f"  Concentration Risk : {assessment.get('concentration_risk')}")
    logger.info(f"  Volatility Risk    : {assessment.get('volatility_risk')}")
    logger.info(f"  Flagged Tickers    : {assessment.get('flagged_tickers')}")
    logger.info(f"  Risk Summary       : {assessment.get('risk_summary')}")
    logger.info(f"  Tokens             : {assessment.get('prompt_tokens')} in / {assessment.get('output_tokens')} out")

    for td in assessment.get("ticker_details", []):
        flags = ", ".join(td.get("risk_flags", [])) or "none"
        logger.info(
            f"    {td['ticker']:6s}  risk_score={td.get('risk_score'):2d}  "
            f"max_pos={td.get('max_position_pct'):5.1f}%  flags=[{flags}]"
        )

    # A run missing ticker coverage is degraded, not failed: the pinned tickers
    # are excluded downstream. Say so loudly — this is easy to miss in a log tail.
    if not assessment.get("coverage_complete", True):
        missing = assessment.get("missing_tickers") or []
        logger.warning("")
        logger.warning("  " + "!" * 56)
        logger.warning(f"  DEGRADED RUN — risk coverage incomplete")
        if assessment.get("response_truncated"):
            logger.warning(f"  Model response was TRUNCATED at max_tokens.")
        logger.warning(
            f"  {len(missing)} of {ctx['total_tickers']} ticker(s) unassessed and "
            f"force-excluded: {', '.join(missing)}"
        )
        logger.warning("  " + "!" * 56)

    logger.info(f"\n[3/3] {'Skipping Supabase save (dry-run)' if args.dry_run else 'Saving to Supabase...'}")
    save_risk_assessment(
        client=client,
        run_id=run_id,
        analyst_run_id=analyst_run_id or ctx.get("source_run_id", "unknown"),
        ctx=ctx,
        assessment=assessment,
        dry_run=args.dry_run,
    )

    logger.info("\n  Risk assessment complete.")
    logger.info("=" * 60)


if __name__ == "__main__":
    run()