"""
run_regime.py — Orblex Industries Market Regime Detector entry point.

Deterministic (non-LLM) stage. Fetches SPY / QQQ / VIX, classifies the
market regime (BULL / CHOPPY / CORRECTION / BEAR), and writes the result
to Supabase. The Allocator reads the latest regime and treats its
risk_floor as a hard constraint when sizing the cash reserve.

Runs in Stage 0 (parallel with macro + earnings — no dependencies).

Usage:
    python run_regime.py
    python run_regime.py --run-id pipeline-2026-06-09-abc123
    python run_regime.py --dry-run
"""

import argparse
import logging
import sys
import uuid
from datetime import datetime, timezone

from config import load_config
from loader import get_client
from regime_detector import detect_regime
from regime_saver import save_regime

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
        logging.FileHandler("regime.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Orblex Industries Market Regime Detector")
    parser.add_argument(
        "--run-id",
        help="Shared pipeline run ID (default: auto-generated)",
        default=None,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print regime output, skip Supabase save",
    )
    return parser.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    args   = parse_args()
    run_id = args.run_id or f"regime-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    try:
        cfg = load_config()
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("Orblex Industries — Market Regime Detector")
    logger.info(f"Run ID   : {run_id}")
    logger.info(f"Dry run  : {args.dry_run}")
    logger.info("=" * 60)

    client = get_client(cfg.supabase_url, cfg.supabase_key)

    # ── Step 1: Detect regime ─────────────────────────────────────────────────
    logger.info("\n[1/2] Detecting market regime from benchmark data...")
    result = detect_regime()
    if result is None:
        logger.error("  Regime detection failed (no SPY data). Check logs above.")
        sys.exit(1)

    logger.info(
        f"\n  -- REGIME DETECTION RESULTS --\n"
        f"  Regime           : {result['regime']}\n"
        f"  Risk floor       : {result['risk_floor'] or 'none'}\n"
        f"  SPY vs 200MA     : {result['spy_vs_ma200_pct']:+.2f}%\n"
        f"  SPY drawdown     : {result['spy_drawdown_pct']:+.2f}% (52-week high)\n"
        f"  Death cross      : {result['death_cross']}\n"
        f"  QQQ vs 200MA     : "
        f"{('%+.2f%%' % result['qqq_vs_ma200_pct']) if result['qqq_vs_ma200_pct'] is not None else 'n/a'}\n"
        f"  VIX              : {result['vix_close'] if result['vix_close'] is not None else 'n/a'}\n"
        f"  Rules fired      : {', '.join(result['rules_fired']) or 'none'}\n"
        f"\n  {result['rationale']}"
    )

    # ── Step 2: Save to Supabase ──────────────────────────────────────────────
    logger.info(f"\n[2/2] {'Skipping save (dry-run)' if args.dry_run else 'Saving to Supabase...'}")
    success = save_regime(client, run_id, result, dry_run=args.dry_run)

    if success:
        logger.info("\n  Regime detection complete.")
    else:
        logger.error("\n  Failed to save regime.")
        sys.exit(1)

    logger.info("=" * 60)


if __name__ == "__main__":
    run()