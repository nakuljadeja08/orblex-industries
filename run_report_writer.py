"""
run_report_writer.py — Orblex Industries Report Writer Agent entry point.

Reads the full pipeline output from Supabase (Analyst → Risk → Allocator)
and synthesizes a structured, auditable final report.

Usage:
    python run_report_writer.py                              # use latest allocation run
    python run_report_writer.py --alloc-run-id alloc-...    # specific allocation run
    python run_report_writer.py --dry-run                   # print output, skip save
"""

import argparse
import logging
import sys
import uuid
from datetime import datetime

from config import load_config
from loader import get_client
from report_writer_context_builder import build_report_context
from report_writer_agent import write_report
from portfolio_stance import stance, why as stance_why
from report_writer_saver import save_report

# ── Logging ───────────────────────────────────────────────────────────────────

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
        logging.FileHandler("report_writer.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Orblex Industries Report Writer Agent")
    parser.add_argument(
        "--alloc-run-id",
        help="Allocation run_id to use (default: latest in Supabase)",
        default=None,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print report output, skip Supabase save",
    )
    return parser.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    args   = parse_args()
    run_id = f"report-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    try:
        cfg = load_config()
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("Orblex Industries  — Report Writer Agent")
    logger.info(f"Run ID   : {run_id}")
    logger.info(f"Dry run  : {args.dry_run}")
    logger.info("=" * 60)

    client = get_client(cfg.supabase_url, cfg.supabase_key)

    # ── Step 1: Build report context ──────────────────────────────────────────
    logger.info("\n[1/3] Building report context from pipeline outputs...")
    ctx = build_report_context(client, alloc_run_id=args.alloc_run_id)
    if ctx is None:
        logger.error("  No allocation plan found. Run run_allocator.py first.")
        sys.exit(1)

    logger.info(
        f"  Market risk    : {ctx['market_risk_level']}\n"
        f"  Invested       : {ctx['invested_pct']}%  |  Cash: {ctx['cash_pct']}%\n"
        f"  Tickers        : {', '.join(t['ticker'] for t in ctx['ticker_summaries'])}"
    )

    # ── Step 2: Run Report Writer Agent ───────────────────────────────────────
    logger.info("\n[2/3] Running Claude Report Writer Agent...")
    report = write_report(ctx)
    if report is None:
        logger.error("  Report Writer Agent failed. Check logs above.")
        sys.exit(1)

    # The stance is a rule, not a judgement: set it from portfolio_stance.py (the
    # rules the prompt states) and record when the model's own label differed.
    tickers = ctx.get("ticker_summaries") or []
    n_flag = len(ctx.get("flagged_tickers") or [])
    rule_stance = stance(ctx.get("market_risk_level"), ctx.get("cash_pct"), n_flag, len(tickers),
                         sum(1 for t in tickers if t.get("signal") == "BUY"))
    if rule_stance and report.get("portfolio_stance") != rule_stance:
        note = (f"Stance set to {rule_stance} by rule ({stance_why(ctx.get('market_risk_level'), ctx.get('cash_pct'), n_flag, len(tickers))}); "
                f"the model had labelled it {report.get('portfolio_stance')}.")
        logger.info(f"  {note}")
        report["portfolio_stance"] = rule_stance
        report["watchdog_notes"] = (f"{report.get('watchdog_notes') or ''} {note}").strip()

    # Print report highlights
    logger.info("\n  -- REPORT HIGHLIGHTS --")
    logger.info(f"  Stance     : {report.get('portfolio_stance')}")
    logger.info(f"  Confidence : {report.get('confidence_level')}")
    logger.info(f"  Themes     : {', '.join(report.get('key_themes', []))}")
    logger.info(f"\n  Executive Summary:")
    logger.info(f"  {report.get('executive_summary', '')}")
    logger.info(f"\n  Watchdog Notes: {report.get('watchdog_notes', 'None.')}")
    logger.info(
        f"\n  Tokens: {report['prompt_tokens']} in / {report['output_tokens']} out"
    )

    # ── Step 3: Save to Supabase ──────────────────────────────────────────────
    logger.info(f"\n[3/3] {'Skipping save (dry-run)' if args.dry_run else 'Saving to Supabase...'}")
    success = save_report(client, run_id, ctx, report, dry_run=args.dry_run)

    if success:
        logger.info("\n  Final report complete.")
    else:
        logger.error("\n  Failed to save report.")
        sys.exit(1)

    logger.info("=" * 60)

    return run_id, report


if __name__ == "__main__":
    run()
