"""
run_universe.py — pipeline stage −1 (swing mode): choose today's analysis list
from the agentic account's latest balance (universe_selector.py).

Reads the latest row of account_snapshots (saved by the morning trade-plan task
from Robinhood, read-only). Prices come from the configured data provider. The
result is written to universe_runs (one row per ticker considered, with the
reason) and the chosen tickers to --out, which run_all.py exports as TICKERS so
every later stage analyses exactly that list.

  python run_universe.py --run-id pipeline-... --out today_tickers.txt
  python run_universe.py --dry-run                 # print the selection only
  UNIVERSE_EQUITY=2500 python run_universe.py --dry-run   # what-if balance

Outside swing mode, or with no account balance from the last
SNAPSHOT_MAX_AGE_DAYS, the full core universe is used and no sleeve is added.
"""

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logger = logging.getLogger(__name__)

SNAPSHOT_MAX_AGE_DAYS = 7


def _latest_account(sb) -> tuple[float | None, set, str | None]:
    """(equity, held tickers, as_of) from account_snapshots, or (None, ∅, None)."""
    override = os.getenv("UNIVERSE_EQUITY", "").strip()
    if override:
        return float(override), set(), "UNIVERSE_EQUITY override"
    try:
        r = (sb.table("account_snapshots").select("snapshot_date,equity,positions")
             .order("as_of", desc=True).limit(1).execute().data)
    except Exception as exc:
        logger.warning(f"  account_snapshots unreadable ({str(exc)[:80]}) — full universe.")
        return None, set(), None
    if not r:
        return None, set(), None
    snap = r[0]
    if date.fromisoformat(str(snap["snapshot_date"])[:10]) < date.today() - timedelta(days=SNAPSHOT_MAX_AGE_DAYS):
        logger.warning(f"  Latest account snapshot is from {snap['snapshot_date']} — too old; full universe.")
        return None, set(), None
    held = {p["ticker"].upper() for p in (snap.get("positions") or []) if float(p.get("qty") or 0) > 0}
    return float(snap["equity"]), held, str(snap["snapshot_date"])


def _metrics(tickers: list[str]) -> dict:
    """Swing indicators + 20-day dollar volume per ticker, from the data provider."""
    from fetcher import fetch_prices
    from swing_policy import SWING_LOOKBACK_DAYS, compute_swing_indicators

    def get(t):
        try:
            df = fetch_prices(t, days=SWING_LOOKBACK_DAYS)
            if df is None or df.empty:
                return t, None
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
            return t, df
        except Exception:
            return t, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        frames = dict(pool.map(get, list(dict.fromkeys(tickers + ["SPY"]))))
    spy = frames.pop("SPY", None)
    out = {}
    for t, df in frames.items():
        if df is None or len(df) < 25:
            out[t] = {}
            continue
        ind = compute_swing_indicators(df, spy)
        tail = df.tail(20)
        out[t] = {
            "price": ind.get("latest_close"), "atr": ind.get("atr_14"), "atr_pct": ind.get("atr_pct"),
            "trend": ind.get("trend"), "price_vs_ma50_pct": ind.get("price_vs_ma50_pct"),
            "rs20": ind.get("rs_vs_spy_20d_pct"),
            "dollar_vol": float((tail["close"].astype(float) * tail["volume"].astype(float)).mean()),
        }
    return out


def main():
    ap = argparse.ArgumentParser(description="Swing universe selection from the account balance")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--out", help="Write the chosen tickers here, comma-separated")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleeve-size", type=int, default=int(os.getenv("UNIVERSE_SLEEVE_SIZE", "8")))
    args = ap.parse_args()

    from config import _read_universe_file, load_config
    from instrument_policy import candidate_sectors
    from loader import get_client
    from universe_selector import select_universe

    cfg = load_config()
    core = _read_universe_file() or cfg.tickers
    sb = get_client(cfg.supabase_url, cfg.supabase_key)

    if cfg.trading_mode != "swing":
        chosen = core
        logger.info(f"  TRADING_MODE={cfg.trading_mode}: full universe ({len(core)} tickers).")
        rows = []
    else:
        equity, held, as_of = _latest_account(sb)
        cands = {t: s for t, s in candidate_sectors().items() if t not in set(core)}
        metrics = _metrics(core + sorted(cands) + sorted(held)) if equity is not None else {}
        rows = select_universe(core, cands, held, metrics, equity, args.sleeve_size,
                               max_tickers=int(os.getenv("UNIVERSE_MAX_TICKERS", "30")))
        chosen = [r["ticker"] for r in rows if r["included"]]

        logger.info(f"  Account balance: {'$' + format(equity, ',.2f') if equity is not None else 'none on record'}"
                    f"{f' (as of {as_of})' if as_of else ''}")
        for src in ("HELD", "CORE", "SLEEVE"):
            inc = [r for r in rows if r["source"] == src and r["included"]]
            exc = [r for r in rows if r["source"] == src and not r["included"]]
            if inc or exc:
                logger.info(f"  {src:<6} analysed {len(inc):>2}: {', '.join(r['ticker'] for r in inc) or '-'}")
            if src == "CORE" and exc:
                logger.info(f"         skipped  {len(exc):>2}: " + "; ".join(f"{r['ticker']} ({r['reason']})" for r in exc[:12])
                            + (" …" if len(exc) > 12 else ""))
        logger.info(f"  → {len(chosen)} tickers analysed today (core universe has {len(core)}).")

        if not args.dry_run and args.run_id and rows:
            now = datetime.now(timezone.utc).isoformat()
            try:
                sb.table("universe_runs").upsert([{
                    "run_id": args.run_id, "run_date": date.today().isoformat(),
                    **{k: r[k] for k in ("ticker", "source", "included", "reason", "rank", "price", "atr_pct",
                                         "dollar_vol_m", "max_qty", "rs20", "sector")},
                    "created_at": now,
                } for r in rows], on_conflict="run_id,ticker").execute()
            except Exception as exc:
                logger.warning(f"  universe_runs not saved ({str(exc)[:100]}) — apply universe_schema.sql.")

    if not chosen:
        logger.error("  Empty selection — refusing to hand an empty universe to the pipeline.")
        sys.exit(1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(",".join(chosen))


if __name__ == "__main__":
    main()
