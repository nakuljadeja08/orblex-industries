"""
run_agent.py — orchestrates the Orblex Industries analyst agent

Usage:
    python run_agent.py                    # analyse all tickers in .env
    python run_agent.py AAPL TSLA          # override tickers via CLI
    python run_agent.py --dry-run          # print analysis, don't save to Supabase
    python run_agent.py --run-id <id>      # use a specific run_id (set by run_all.py)
    python run_agent.py --mode swing       # 1–10 day swing plans (default: TRADING_MODE env)
"""

import argparse
import logging
import sys
import uuid
from datetime import datetime, timezone

from config import load_config, TRADING_MODES
from loader import get_client
from context_builder import build_context, build_market_context, query_spy_prices
from analyst_agent import analyse
from performance_context import (
    build_performance_summary, build_swing_feedback, format_analyst_feedback, format_swing_feedback,
)
from report_saver import save_report, save_run_log

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
        logging.FileHandler("agent.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="Orblex Industries Analyst Agent")
    parser.add_argument("tickers", nargs="*", help="Tickers to analyse (overrides .env)")
    parser.add_argument("--dry-run", action="store_true", help="Print reports, skip Supabase save")
    parser.add_argument("--run-id",  default=None,        help="Shared pipeline run_id (set by run_all.py)")
    parser.add_argument("--mode",    choices=TRADING_MODES, default=None,
                        help="Analyst horizon (overrides TRADING_MODE)")
    return parser.parse_args()


def run():
    args = parse_args()

    # Use the shared pipeline run_id if provided, otherwise generate own
    run_id = args.run_id or f"agent-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    try:
        cfg = load_config()
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)

    tickers = [t.upper() for t in args.tickers] if args.tickers else cfg.tickers
    mode = args.mode or cfg.trading_mode

    logger.info("=" * 60)
    logger.info(f"Orblex Industries Analyst Agent — run {run_id}")
    logger.info(f"Tickers  : {', '.join(tickers)}")
    logger.info(f"Mode     : {mode}")
    logger.info(f"Dry run  : {args.dry_run}")
    logger.info("=" * 60)

    client = get_client(cfg.supabase_url, cfg.supabase_key)

    # Read the feedback loop once for the whole run: how prior signals actually
    # played out (Stage 9 backtest). None on a fresh system with no backtest yet.
    perf_summary = build_performance_summary(client, tickers=tickers)
    logger.info(
        f"Backtest feedback: {'loaded (run ' + perf_summary['backtest_run_id'] + ')' if perf_summary else 'unavailable'}"
    )

    # Portfolio-wide Stage 0 context (regime + macro) — identical for every
    # ticker, so fetch it once and inject into each ticker's context.
    market_context = build_market_context(client)
    _reg = (market_context.get("regime") or {}).get("regime", "N/A")
    _mac = (market_context.get("macro") or {}).get("macro_regime", "N/A")
    logger.info(f"Market context: regime={_reg}  macro={_mac}")

    # SPY bars for swing relative strength, and swing trade outcomes (in R) as
    # the swing analyst's feedback — once per run.
    spy_df = query_spy_prices(client) if mode == "swing" else None
    swing_summary = build_swing_feedback(client) if mode == "swing" else None

    reports_written = 0
    failed_tickers  = []

    for ticker in tickers:
        logger.info(f"\n-- {ticker} ------------------------------------------")

        ctx = build_context(client, ticker, mode=mode, spy_df=spy_df)
        if ctx is None:
            logger.warning(f"  Skipping {ticker} — not enough data in stock_prices")
            failed_tickers.append(ticker)
            continue

        ctx["performance_feedback"] = (format_swing_feedback(swing_summary, ticker) if mode == "swing"
                                       else format_analyst_feedback(perf_summary, ticker))
        ctx["market_context"]       = market_context

        analysis = analyse(ctx)
        if analysis is None:
            logger.error(f"  Skipping {ticker} — agent returned no result")
            failed_tickers.append(ticker)
            continue

        logger.info(f"  Signal    : {analysis['signal']} ({analysis['signal_strength']})")
        if analysis.get("entry_price"):
            logger.info(f"  Plan      : {analysis.get('setup_type')} entry {analysis['entry_price']} "
                        f"stop {analysis['stop_price']} target {analysis['target_price']} "
                        f"R:R {analysis.get('risk_reward')} horizon {analysis['horizon_days']}d")
        logger.info(f"  Summary   : {analysis['summary'][:120]}...")
        logger.info(f"  Anomalies : {len(analysis.get('anomalies', []))} detected")
        logger.info(f"  Tokens    : {analysis.get('prompt_tokens')} in / {analysis.get('output_tokens')} out")

        if not args.dry_run:
            ok = save_report(client, run_id, ticker, analysis, ctx)
            if ok:
                reports_written += 1
            else:
                failed_tickers.append(ticker)
        else:
            logger.info(f"  [DRY RUN] Skipping Supabase save for {ticker}")
            reports_written += 1

    status = (
        "success" if not failed_tickers else
        "partial" if reports_written > 0 else
        "failed"
    )

    if not args.dry_run:
        save_run_log(
            client,
            run_id=run_id,
            tickers=tickers,
            reports_written=reports_written,
            status=status,
            error_message=f"Failed: {failed_tickers}" if failed_tickers else None,
        )

    logger.info("=" * 60)
    logger.info(f"Run complete | status={status.upper()} | reports={reports_written}/{len(tickers)}")
    logger.info("=" * 60)

    sys.exit(0 if status != "failed" else 1)


if __name__ == "__main__":
    run()