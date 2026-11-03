"""
run_drift.py — Orblex Industries Signal Drift Tracker

Compares today's analyst signals against historical signal_history to detect
flips, computes drift metrics, and writes to signal_history + signal_drift.

Called by run_all.py (Stage 3.5) after run_agent.py completes.

Design principles:
  - signal_history is append-only — never updated, never deleted
  - signal_drift only records actual flips (signal changed vs prior run)
  - flip_severity is deterministic: direction pair + strength delta
  - is_chronic fires when a ticker flips >= 3 times in 7 days
  - All Supabase writes use upsert on (run_id, ticker) — safe to re-run

Usage:
  python run_drift.py                      # auto-detects latest analyst run
  python run_drift.py --run-id <id>        # target a specific run
  python run_drift.py --dry-run            # print analysis, skip DB writes
"""

import os, sys, argparse, logging
from datetime import date, timedelta
from typing import Optional

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

logger = logging.getLogger(__name__)

# ── Severity matrix ───────────────────────────────────────────────────────────
# MAJOR:    direction reversal involving BUY↔SELL
# MODERATE: any flip involving HOLD (BUY→HOLD, HOLD→SELL, etc.)
# MINOR:    strength-only change on same signal (not tracked as flip)

_SEVERITY = {
    ("BUY",  "SELL"): "MAJOR",
    ("SELL", "BUY"):  "MAJOR",
    ("BUY",  "HOLD"): "MODERATE",
    ("HOLD", "BUY"):  "MODERATE",
    ("SELL", "HOLD"): "MODERATE",
    ("HOLD", "SELL"): "MODERATE",
}

CHRONIC_THRESHOLD_7D  = 3   # flips in 7 days → is_chronic = True
CHRONIC_THRESHOLD_30D = 5   # informational


def _severity(signal_from: str, signal_to: str) -> str:
    return _SEVERITY.get((signal_from, signal_to), "MODERATE")


# ── History writer ────────────────────────────────────────────────────────────

def write_signal_history(
    client: Client,
    run_id: str,
    reports: list[dict],
    contexts: dict[str, dict],
    dry_run: bool = False,
) -> int:
    """
    Appends one signal_history row per ticker for this run.

    Args:
        reports:   list of agent_reports dicts (signal, signal_strength, ticker)
        contexts:  dict keyed by ticker → context dict (indicators sub-dict)
                   pass {} if context isn't available — indicators will be NULL
    Returns:
        Number of rows written.
    """
    rows = []
    today = date.today().isoformat()

    for r in reports:
        ticker  = r["ticker"]
        ctx_ind = contexts.get(ticker, {}).get("indicators", {})

        rows.append({
            "run_id":          run_id,
            "ticker":          ticker,
            "report_date":     today,
            "signal":          r["signal"],
            "signal_strength": r.get("signal_strength"),
            "close_price":     ctx_ind.get("latest_close"),
            "rsi_14":          ctx_ind.get("rsi_14"),
            "ma_5":            ctx_ind.get("ma_5"),
            "ma_20":           ctx_ind.get("ma_20"),
            "volatility_ann":  ctx_ind.get("volatility_ann_pct"),
            "volume_ratio":    ctx_ind.get("volume_ratio"),
        })

    if dry_run:
        logger.info(f"  [DRY RUN] Would write {len(rows)} rows to signal_history")
        for r in rows:
            logger.info(f"    {r['ticker']:<6}  {r['signal']:<4}  ({r['signal_strength']})")
        return len(rows)

    try:
        client.table("signal_history").upsert(
            rows, on_conflict="run_id,ticker"
        ).execute()
        logger.info(f"  [OK] signal_history — {len(rows)} rows written")
        return len(rows)
    except Exception as exc:
        logger.error(f"  [FAIL] signal_history write failed: {exc}")
        return 0


# ── Drift detector ────────────────────────────────────────────────────────────

def _fetch_previous_signal(
    client: Client,
    ticker: str,
    before_run_id: str,
) -> Optional[dict]:
    """
    Fetches the most recent signal_history row for this ticker
    that is NOT from the current run.
    Returns None if no prior history exists.
    """
    try:
        resp = (
            client.table("signal_history")
            .select("signal,signal_strength,report_date")
            .eq("ticker", ticker)
            .neq("run_id", before_run_id)
            .order("report_date", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None
    except Exception as exc:
        logger.warning(f"  Could not fetch prior signal for {ticker}: {exc}")
        return None


def _fetch_flip_counts(
    client: Client,
    ticker: str,
) -> tuple[int, int]:
    """
    Returns (flip_count_7d, flip_count_30d) from signal_drift for this ticker.
    Counts flips in the last 7 and 30 days respectively.
    """
    today = date.today()
    since_7d  = (today - timedelta(days=7)).isoformat()
    since_30d = (today - timedelta(days=30)).isoformat()

    try:
        resp = (
            client.table("signal_drift")
            .select("flip_date")
            .eq("ticker", ticker)
            .gte("flip_date", since_30d)
            .execute()
        )
        rows = resp.data or []
        count_7d  = sum(1 for r in rows if r["flip_date"] >= since_7d)
        count_30d = len(rows)
        return count_7d, count_30d
    except Exception as exc:
        logger.warning(f"  Could not fetch flip counts for {ticker}: {exc}")
        return 0, 0


def detect_and_write_drift(
    client: Client,
    run_id: str,
    reports: list[dict],
    dry_run: bool = False,
) -> dict:
    """
    Core drift detection. For each ticker in reports:
      1. Fetch previous signal from signal_history
      2. If signal changed → compute severity, flip counts, is_chronic
      3. Write to signal_drift

    Returns a summary dict:
      {
        "flips_detected": int,
        "chronic_tickers": [str],
        "major_flips": [{"ticker", "from", "to"}],
        "drift_rows": [dict],   # all written rows
      }
    """
    today     = date.today().isoformat()
    drift_rows = []
    chronic_tickers = []
    major_flips     = []

    for r in reports:
        ticker       = r["ticker"]
        signal_now   = r["signal"]
        strength_now = r.get("signal_strength")
        price_now    = r.get("close_price")   # optional — enriched by run_drift.py

        prev = _fetch_previous_signal(client, ticker, run_id)

        if prev is None:
            logger.info(f"  {ticker:<6}  No prior history — first run, skipping drift check")
            continue

        signal_prev   = prev["signal"]
        strength_prev = prev.get("signal_strength")

        if signal_now == signal_prev:
            logger.info(f"  {ticker:<6}  {signal_now:<4}  No flip (same as previous)")
            continue

        # Signal changed — compute drift metrics
        severity = _severity(signal_prev, signal_now)
        count_7d, count_30d = _fetch_flip_counts(client, ticker)

        # +1 for the current flip
        count_7d  += 1
        count_30d += 1
        is_chronic = count_7d >= CHRONIC_THRESHOLD_7D

        logger.info(
            f"  {ticker:<6}  FLIP  {signal_prev} → {signal_now}  "
            f"severity={severity}  flips_7d={count_7d}  chronic={is_chronic}"
        )

        drift_row = {
            "run_id":        run_id,
            "ticker":        ticker,
            "flip_date":     today,
            "signal_from":   signal_prev,
            "signal_to":     signal_now,
            "strength_from": strength_prev,
            "strength_to":   strength_now,
            "price_at_flip": price_now,
            "flip_severity": severity,
            "flip_count_7d": count_7d,
            "flip_count_30d": count_30d,
            "is_chronic":    is_chronic,
        }
        drift_rows.append(drift_row)

        if severity == "MAJOR":
            major_flips.append({
                "ticker": ticker,
                "from":   signal_prev,
                "to":     signal_now,
            })
        if is_chronic:
            chronic_tickers.append(ticker)

    if not drift_rows:
        logger.info("  No signal flips detected this run.")
    elif dry_run:
        logger.info(f"  [DRY RUN] Would write {len(drift_rows)} drift rows:")
        for dr in drift_rows:
            logger.info(
                f"    {dr['ticker']:<6}  {dr['signal_from']} → {dr['signal_to']}"
                f"  {dr['flip_severity']}  chronic={dr['is_chronic']}"
            )
    else:
        try:
            client.table("signal_drift").upsert(
                drift_rows, on_conflict="run_id,ticker"
            ).execute()
            logger.info(f"  [OK] signal_drift — {len(drift_rows)} flip rows written")
        except Exception as exc:
            logger.error(f"  [FAIL] signal_drift write failed: {exc}")

    return {
        "flips_detected":  len(drift_rows),
        "chronic_tickers": chronic_tickers,
        "major_flips":     major_flips,
        "drift_rows":      drift_rows,
    }


# ── Convenience: full run ─────────────────────────────────────────────────────

def run_drift_tracking(
    client: Client,
    run_id: str,
    reports: list[dict],
    contexts: dict[str, dict] = None,
    dry_run: bool = False,
) -> dict:
    """
    Convenience wrapper that runs both steps in order:
      1. write_signal_history  — append this run's signals
      2. detect_and_write_drift — compare and write flips

    Args:
        reports:  list of dicts from agent_reports (ticker, signal, signal_strength)
        contexts: optional dict[ticker → context] for enriching history with indicators
    """
    contexts = contexts or {}
    logger.info(f"\n[Drift Tracker] run_id={run_id}  tickers={len(reports)}")

    logger.info("\n  Step 1/2 — Writing signal history...")
    written = write_signal_history(client, run_id, reports, contexts, dry_run)

    logger.info("\n  Step 2/2 — Detecting signal drift...")
    summary = detect_and_write_drift(client, run_id, reports, dry_run)

    summary["history_rows_written"] = written

    # Log digest
    logger.info(f"\n  ── DRIFT SUMMARY ──")
    logger.info(f"  History rows written : {written}")
    logger.info(f"  Flips detected       : {summary['flips_detected']}")
    logger.info(f"  Major flips          : {[f['ticker'] for f in summary['major_flips']]}")
    logger.info(f"  Chronic tickers      : {summary['chronic_tickers']}")

    return summary


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(description="Orblex Industries — Signal Drift Tracker")
    parser.add_argument("--run-id",  default=None, help="Analyst run ID to process (auto-detects latest if omitted)")
    parser.add_argument("--dry-run", action="store_true", help="Print analysis, skip DB writes")
    args = parser.parse_args()

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Auto-discover the latest analyst run if no run-id provided
    run_id = args.run_id
    if not run_id:
        resp = (
            sb.table("agent_reports")
            .select("run_id")
            .order("report_date", desc=True)
            .limit(1)
            .execute()
        )
        if not resp.data:
            print("No agent_reports found — run run_agent.py first.")
            sys.exit(1)
        run_id = resp.data[0]["run_id"]

    # Fetch all reports for this run
    resp = (
        sb.table("agent_reports")
        .select("ticker,signal,signal_strength")
        .eq("run_id", run_id)
        .execute()
    )
    reports = resp.data or []

    if not reports:
        print(f"No reports found for run_id={run_id}")
        sys.exit(1)

    print(f"\n{'='*55}")
    print(f"  Orblex Industries — Signal Drift Tracker")
    print(f"  Run ID  : {run_id}")
    print(f"  Tickers : {', '.join(r['ticker'] for r in reports)}")
    print(f"  Dry run : {args.dry_run}")
    print(f"{'='*55}\n")

    summary = run_drift_tracking(sb, run_id, reports, dry_run=args.dry_run)

    print(f"\n{'='*55}")
    print(f"  Flips detected : {summary['flips_detected']}")
    if summary["major_flips"]:
        for f in summary["major_flips"]:
            print(f"  MAJOR FLIP     : {f['ticker']}  {f['from']} → {f['to']}")
    if summary["chronic_tickers"]:
        print(f"  Chronic        : {summary['chronic_tickers']}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()