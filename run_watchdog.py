"""
run_watchdog.py — Orblex Industries Watchdog Agent entry point.

Audits the full pipeline cycle for internal consistency and flags violations.

Usage:
    python run_watchdog.py                                  # use latest report run
    python run_watchdog.py --report-run-id report-...       # specific report run
    python run_watchdog.py --dry-run                        # print output, skip save
"""

import argparse
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

from config import load_config
from loader import get_client
from watchdog_context_builder import build_watchdog_context
from watchdog_agent import audit_pipeline
from watchdog_rules import audit as audit_rules
from watchdog_saver import save_watchdog_audit

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
        logging.FileHandler("watchdog.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Orblex Industries Watchdog Agent")
    parser.add_argument(
        "--report-run-id",
        help="Report run_id to audit (default: latest in Supabase)",
        default=None,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print audit output, skip Supabase save",
    )
    return parser.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    args   = parse_args()
    run_id = f"watchdog-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    try:
        cfg = load_config()
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("Orblex Industries  — Watchdog Agent")
    logger.info(f"Run ID   : {run_id}")
    logger.info(f"Dry run  : {args.dry_run}")
    logger.info("=" * 60)

    client = get_client(cfg.supabase_url, cfg.supabase_key)

    # ── Step 1: Build watchdog context ────────────────────────────────────────
    logger.info("\n[1/3] Building audit context from full pipeline...")
    ctx = build_watchdog_context(client, report_run_id=args.report_run_id)
    if ctx is None:
        logger.error("  No final report found. Run run_report_writer.py first.")
        sys.exit(1)

    # Print pre-computed checks
    logger.info("\n  Pre-computed consistency checks:")
    for c in ctx["consistency_checks"]:
        status_icon = "[PASS]" if c["status"] == "PASS" else ("[WARN]" if c["status"] == "WARN" else "[FAIL]")
        logger.info(f"  {status_icon} {c['rule']}: {c['detail']}")

    # ── Step 2: Run Watchdog Agent ────────────────────────────────────────────
    # WATCHDOG_ENGINE=rules (default): watchdog_rules.py, deterministic and free.
    # WATCHDOG_ENGINE=llm rolls back to the Claude Watchdog.
    if os.getenv("WATCHDOG_ENGINE", "rules").strip().lower() == "llm":
        logger.info("\n[2/3] Running Claude Watchdog Agent...")
        audit = audit_pipeline(ctx)
    else:
        logger.info("\n[2/3] Auditing with rules (watchdog_rules.py)...")
        audit = audit_rules(ctx)
    if audit is None:
        logger.error("  Watchdog Agent failed. Check logs above.")
        sys.exit(1)

    # Print audit results
    health = audit.get("pipeline_health_score", "?")
    status = audit.get("overall_status", "UNKNOWN")
    status_icon = "[CLEAN]" if status == "CLEAN" else ("[WARN]" if status == "WARNINGS" else "[VIOLATION]")

    logger.info(f"\n  -- WATCHDOG AUDIT RESULTS --")
    logger.info(f"  {status_icon} Status         : {status}")
    logger.info(f"  Health score    : {health}/10")
    logger.info(f"  Audit summary   : {audit.get('audit_summary', '')}")

    issues = audit.get("agent_consistency_issues", [])
    if issues:
        logger.info(f"\n  Issues found ({len(issues)}):")
        for i in issues:
            logger.info(f"    [{i['severity']}] {i['issue_type']} — {i.get('ticker', 'PORTFOLIO')}: {i.get('description', '')}")
    else:
        logger.info("\n  No consistency issues found.")

    recs = audit.get("recommendations", [])
    if recs:
        logger.info("\n  Recommendations:")
        for r in recs:
            logger.info(f"    >> {r}")

    logger.info(
        f"\n  Tokens: {audit['prompt_tokens']} in / {audit['output_tokens']} out"
    )

    # ── Step 3: Save to Supabase ──────────────────────────────────────────────
    logger.info(f"\n[3/3] {'Skipping save (dry-run)' if args.dry_run else 'Saving to Supabase...'}")
    success = save_watchdog_audit(client, run_id, ctx, audit, dry_run=args.dry_run)

    if success:
        logger.info(f"\n  Watchdog audit complete — pipeline health: {health}/10")
    else:
        logger.error("\n  Failed to save watchdog audit.")
        sys.exit(1)

    logger.info("=" * 60)

    return run_id, audit


if __name__ == "__main__":
    run()
