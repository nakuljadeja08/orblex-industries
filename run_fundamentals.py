"""
run_fundamentals.py — Orblex Industries Fundamentals Snapshot entry point.

Deterministic (non-LLM) Stage 0 stage. Pulls valuation fundamentals from
yfinance .info for every ticker in the universe and upserts a daily snapshot
into stock_fundamentals. The Analyst Agent reads the latest snapshot via
context_builder so signals can weigh valuation, not just momentum.

Notes:
  - ETFs (XLP/XLU/GLD/SH) have most fields null — stored as-is, harmless.
  - yfinance .info is the slowest yfinance call; a short sleep between
    tickers avoids rate limiting.
  - Data freshness: .info values update on Yahoo's schedule, not real-time.

Usage:
    python run_fundamentals.py
    python run_fundamentals.py --run-id pipeline-2026-06-10-abc123
    python run_fundamentals.py --dry-run
    python run_fundamentals.py --tickers AAPL,NVDA
"""

import argparse
import logging
import sys
import time
import uuid
from datetime import date, datetime
from typing import Optional

import yfinance as yf

from config import load_config
from loader import get_client

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
        logging.FileHandler("fundamentals.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

TABLE        = "stock_fundamentals"
SLEEP_SECS   = 0.8     # be polite to yfinance between .info calls

# yfinance .info key → our column
FIELD_MAP = {
    "trailingPE":            "trailing_pe",
    "forwardPE":             "forward_pe",
    "trailingPegRatio":      "peg_ratio",
    "priceToSalesTrailing12Months": "price_to_sales",
    "priceToBook":           "price_to_book",
    "dividendYield":         "dividend_yield",
    "profitMargins":         "profit_margin",
    "revenueGrowth":         "revenue_growth",
    "earningsGrowth":        "earnings_growth",
    "beta":                  "beta",
    "marketCap":             "market_cap",
    "fiftyTwoWeekHigh":      "fifty_two_wk_high",
    "fiftyTwoWeekLow":       "fifty_two_wk_low",
    "targetMeanPrice":       "target_mean_price",
    "recommendationKey":     "recommendation_key",
}

NUMERIC_COLS = {c for c in FIELD_MAP.values()} - {"recommendation_key", "market_cap"}


def _clean(col: str, val):
    """Coerce yfinance values: reject non-numerics, infinities, absurd PEs."""
    if val is None:
        return None
    if col == "recommendation_key":
        return str(val)
    if col == "market_cap":
        try:
            return int(val)
        except (TypeError, ValueError):
            return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):   # NaN / inf
        return None
    return round(f, 5)


def fetch_fundamentals(ticker: str, run_id: str) -> Optional[dict]:
    """One snapshot row for one ticker, or None on total failure."""
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as e:
        logger.error(f"  [ERR] {ticker}: .info fetch failed: {e}")
        return None

    row = {
        "run_id":        run_id,
        "ticker":        ticker,
        "snapshot_date": date.today().isoformat(),
    }
    populated = 0
    for yf_key, col in FIELD_MAP.items():
        v = _clean(col, info.get(yf_key))
        row[col] = v
        populated += v is not None

    logger.info(f"  [OK] {ticker}: {populated}/{len(FIELD_MAP)} fields populated")
    return row


def run():
    parser = argparse.ArgumentParser(description="Orblex Industries — Fundamentals Snapshot")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tickers", default=None,
                        help="Comma-separated override of the configured universe")
    args = parser.parse_args()

    run_id = args.run_id or f"fund-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    try:
        cfg = load_config()
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)

    tickers = (
        [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        if args.tickers else cfg.tickers
    )
    tickers = list(dict.fromkeys(tickers))   # order-preserving dedupe

    logger.info("=" * 60)
    logger.info("Orblex Industries — Fundamentals Snapshot")
    logger.info(f"Run ID  : {run_id}")
    logger.info(f"Tickers : {len(tickers)}")
    logger.info(f"Dry run : {args.dry_run}")
    logger.info("=" * 60)

    client = get_client(cfg.supabase_url, cfg.supabase_key)

    rows, failed = [], []
    for i, tk in enumerate(tickers, 1):
        logger.info(f"[{i}/{len(tickers)}] {tk}")
        row = fetch_fundamentals(tk, run_id)
        (rows if row else failed).append(row or tk)
        time.sleep(SLEEP_SECS)

    if not rows:
        logger.error("No fundamentals fetched at all — aborting.")
        sys.exit(1)

    if args.dry_run:
        logger.info(f"\n[DRY RUN] Would upsert {len(rows)} rows into {TABLE}")
    else:
        try:
            client.table(TABLE).upsert(rows, on_conflict="ticker,snapshot_date").execute()
            logger.info(f"\nUpserted {len(rows)} rows into {TABLE}")
        except Exception as e:
            logger.error(f"Upsert failed: {e}")
            sys.exit(1)

    if failed:
        logger.warning(f"Failed tickers ({len(failed)}): {', '.join(failed)}")
    logger.info("=" * 60)


if __name__ == "__main__":
    run()