"""
loader.py — upserts fetched data into Supabase tables
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from supabase import create_client, Client

logger = logging.getLogger(__name__)


def get_client(url: str, key: str) -> Client:
    return create_client(url, key)


def upsert_prices(client: Client, df: pd.DataFrame) -> tuple[int, int]:
    """
    Upsert price rows into stock_prices.
    Returns (rows_inserted, rows_skipped).
    The UNIQUE(ticker, date) constraint in Postgres handles deduplication —
    on conflict we update the price fields in case Yahoo revises them.
    """
    if df.empty:
        return 0, 0

    records = df.to_dict(orient="records")

    # Convert date objects → ISO strings for JSON serialisation
    for r in records:
        if hasattr(r["date"], "isoformat"):
            r["date"] = r["date"].isoformat()
        # Convert pandas NA / Int64 NA → None
        for k, v in r.items():
            try:
                if pd.isna(v):
                    r[k] = None
            except (TypeError, ValueError):
                pass

    try:
        # upsert: insert or update on (ticker, date) conflict
        client.table("stock_prices").upsert(
            records,
            on_conflict="ticker,date",
        ).execute()

        logger.info(f"    Upserted {len(records)} price rows")
        return len(records), 0

    except Exception as exc:
        logger.error(f"    Price upsert failed: {exc}")
        return 0, len(records)


def upsert_metadata(client: Client, meta: Optional[dict]) -> None:
    """Upsert one company metadata row."""
    if not meta:
        return
    try:
        client.table("stock_metadata").upsert(
            meta,
            on_conflict="ticker",
        ).execute()
        logger.info(f"    Metadata upserted for {meta['ticker']}")
    except Exception as exc:
        logger.error(f"    Metadata upsert failed: {exc}")


def log_pipeline_run(
    client: Client,
    tickers: list[str],
    rows_inserted: int,
    rows_skipped: int,
    status: str,
    error_message: str = None,
) -> None:
    """Write an audit row to pipeline_runs."""
    try:
        client.table("pipeline_runs").insert({
            "run_at":        datetime.now(timezone.utc).isoformat(),
            "tickers":       tickers,
            "rows_inserted": rows_inserted,
            "rows_skipped":  rows_skipped,
            "status":        status,
            "error_message": error_message,
        }).execute()
    except Exception as exc:
        logger.warning(f"Could not write pipeline run log: {exc}")
