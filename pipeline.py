"""
pipeline.py — main entry point

Usage:
    python pipeline.py                    # uses tickers from .env
    python pipeline.py AAPL TSLA NVDA     # override tickers via CLI args
    python pipeline.py --days 30          # pull last 30 days only
    python pipeline.py --dry-run          # fetch prices but skip Supabase write
"""

import argparse
import logging
import sys

from config import load_config
from fetcher import fetch_prices, fetch_metadata
from loader import get_client, upsert_prices, upsert_metadata, log_pipeline_run

# ── Logging setup ─────────────────────────────────────────────────────────────

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
        logging.FileHandler("pipeline.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ── CLI args ──────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Stock data pipeline → Supabase")
    parser.add_argument(
        "tickers",
        nargs="*",
        help="Tickers to pull (overrides .env TICKERS if provided)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Days of history to pull (overrides .env HISTORICAL_DAYS)",
    )
    parser.add_argument(
        "--skip-metadata",
        action="store_true",
        help="Skip fetching company metadata (faster re-runs)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch prices but skip Supabase writes",
    )
    return parser.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    args = parse_args()

    try:
        cfg = load_config()
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)

    tickers = [t.upper() for t in args.tickers] if args.tickers else cfg.tickers
    days    = args.days if args.days else cfg.historical_days

    logger.info("=" * 60)
    logger.info(f"Orblex Industries Stock Pipeline — starting run")
    logger.info(f"Tickers : {', '.join(tickers)}")
    logger.info(f"History : {days} days")
    logger.info(f"Dry run : {args.dry_run}")
    logger.info("=" * 60)

    if args.dry_run:
        # In dry-run mode, fetch prices and log what would be written
        for ticker in tickers:
            logger.info(f"\n-- {ticker} ------------------------------------------")
            df = fetch_prices(ticker, days=days)
            if df.empty:
                logger.warning(f"  No price data returned for {ticker}")
            else:
                logger.info(f"  [DRY RUN] Would upsert {len(df)} rows for {ticker}")
        logger.info("\n[DRY RUN] No data written to Supabase.")
        return

    # Connect to Supabase
    try:
        client = get_client(cfg.supabase_url, cfg.supabase_key)
        logger.info("Connected to Supabase [OK]")
    except Exception as e:
        logger.error(f"Could not connect to Supabase: {e}")
        sys.exit(1)

    total_inserted = 0
    total_skipped  = 0
    failed_tickers = []

    for ticker in tickers:
        logger.info(f"\n-- {ticker} ------------------------------------------")

        # 1. Prices
        df = fetch_prices(ticker, days=days)
        inserted, skipped = upsert_prices(client, df)
        total_inserted += inserted
        total_skipped  += skipped

        if df.empty:
            failed_tickers.append(ticker)
            continue

        # 2. Metadata (optional)
        if not args.skip_metadata:
            meta = fetch_metadata(ticker)
            upsert_metadata(client, meta)

    # 3. Audit log
    status = (
        "success" if not failed_tickers else
        "partial" if total_inserted > 0 else
        "failed"
    )
    log_pipeline_run(
        client,
        tickers=tickers,
        rows_inserted=total_inserted,
        rows_skipped=total_skipped,
        status=status,
        error_message=f"Failed tickers: {failed_tickers}" if failed_tickers else None,
    )

    logger.info("\n" + "=" * 60)
    logger.info(f"Run complete  |  status: {status.upper()}")
    logger.info(f"Rows inserted : {total_inserted}")
    logger.info(f"Rows skipped  : {total_skipped}")
    if failed_tickers:
        logger.warning(f"Failed tickers: {failed_tickers}")
    logger.info("=" * 60)


def run_pipeline(client, tickers: list, dry_run: bool = False):
    """Callable entry point for run_all.py."""
    from config import load_config
    cfg  = load_config()
    days = cfg.historical_days

    total_inserted = 0
    total_skipped  = 0
    failed_tickers = []

    for ticker in tickers:
        logger.info(f"  Fetching {ticker}...")
        df = fetch_prices(ticker, days=days)

        if df.empty:
            logger.warning(f"  No price data for {ticker}, skipping.")
            failed_tickers.append(ticker)
            continue

        if dry_run:
            logger.info(f"  [DRY RUN] Would upsert {len(df)} rows for {ticker}")
            total_inserted += len(df)
        else:
            inserted, skipped = upsert_prices(client, df)
            total_inserted += inserted
            total_skipped  += skipped
            meta = fetch_metadata(ticker)
            upsert_metadata(client, meta)

    status = (
        "success" if not failed_tickers else
        "partial" if total_inserted > 0 else
        "failed"
    )

    if not dry_run:
        log_pipeline_run(
            client,
            tickers=tickers,
            rows_inserted=total_inserted,
            rows_skipped=total_skipped,
            status=status,
            error_message=f"Failed tickers: {failed_tickers}" if failed_tickers else None,
        )

    logger.info(
        f"  Pipeline done: {total_inserted} inserted, "
        f"{total_skipped} skipped, status={status.upper()}"
    )
    if failed_tickers:
        logger.warning(f"  Failed tickers: {failed_tickers}")


if __name__ == "__main__":
    run()