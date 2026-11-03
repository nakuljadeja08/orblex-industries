"""
run_backtest.py — Orblex Industries Backtesting Runner

Evaluates all historical allocation plans against actual price outcomes
across 1-day, 5-day, and 20-day holding periods.

Results written to:
  backtest_results        — portfolio-level returns per plan per holding period
  backtest_ticker_results — per-ticker returns and signal correctness
  backtest_runs           — audit log of each backtest execution

Usage:
  python run_backtest.py              # run full backtest
  python run_backtest.py --dry-run    # compute but don't write to Supabase
  python run_backtest.py --summary    # print aggregate stats after run
  python run_backtest.py --swing-only # only re-simulate swing trade plans

Swing-mode plans (TRADING_MODE=swing) are scored separately by swing_backtest.py
— a bar-by-bar trade simulation written to swing_backtest_trades.
"""

import argparse
import logging
import sys
import uuid
from datetime import date, datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

from config import load_config
from backtest_engine import run_backtest, HOLDING_PERIODS
from swing_backtest import run_swing_backtest

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _log_swing_summary(summ: dict) -> None:
    """Closed-trade statistics in R (risk units), the swing equivalent of hit rate."""
    o = summ.get("overall")
    if not summ.get("plans"):
        return
    fill = summ.get("fill_rate")
    logger.info(f"  Plans {summ['plans']}  |  fill rate {fill * 100:.0f}%" if fill is not None
                else f"  Plans {summ['plans']}")
    logger.info(f"  Outcomes: {summ['outcomes']}")
    if not o:
        logger.info("  No closed swing trades yet.")
        return
    pf = o["profit_factor"]
    logger.info(f"  Closed {o['n']}  |  win {o['win_rate'] * 100:.0f}%  |  expectancy {o['avg_r']:+.2f}R  |  "
                f"profit factor {pf if pf is not None else 'N/A'}  |  avg hold {o['avg_hold']} sessions")
    for setup, st in (summ.get("by_setup") or {}).items():
        logger.info(f"    {setup:<10} n={st['n']:<3} win {st['win_rate'] * 100:3.0f}%  avg {st['avg_r']:+.2f}R")


def main():
    parser = argparse.ArgumentParser(description="Orblex Industries — Backtesting Runner")
    parser.add_argument("--dry-run", action="store_true", help="Compute but don't write results")
    parser.add_argument("--summary", action="store_true", help="Print aggregate stats after run")
    parser.add_argument("--swing-only", action="store_true", help="Only simulate swing trade plans")
    args = parser.parse_args()

    backtest_run_id = f"backtest-{date.today().isoformat()}-{uuid.uuid4().hex[:6]}"

    logger.info("=" * 60)
    logger.info("  Orblex Industries — Backtesting Engine")
    logger.info(f"  Run ID  : {backtest_run_id}")
    logger.info(f"  Periods : {HOLDING_PERIODS} trading days")
    logger.info(f"  Dry run : {args.dry_run}")
    logger.info("=" * 60)

    cfg    = load_config()
    client = create_client(cfg.supabase_url, cfg.supabase_key)

    logger.info(f"  Tickers (.env) : {', '.join(cfg.tickers)}")

    # ── Swing trade simulation ────────────────────────────────────────────────
    # Independent of the allocation backtest, and never allowed to fail it: the
    # swing tables exist only once swing_schema.sql has been applied.
    try:
        logger.info("\n  Swing trade plans (swing_backtest.py)...")
        _log_swing_summary(run_swing_backtest(client, backtest_run_id, dry_run=args.dry_run)["summary"])
    except Exception as exc:
        logger.warning(f"  Swing backtest skipped: {exc}")
    if args.swing_only:
        return

    # ── Run backtest ──────────────────────────────────────────────────────────
    result = run_backtest(client, backtest_run_id, tickers=cfg.tickers, dry_run=args.dry_run)

    if "error" in result:
        logger.error(f"  Backtest failed: {result['error']}")
        sys.exit(1)

    # ── Write backtest_runs audit row ─────────────────────────────────────────
    if not args.dry_run:
        try:
            client.table("backtest_runs").upsert({
                "run_id":          backtest_run_id,
                "run_date":        date.today().isoformat(),
                "periods_tested":  result["periods_tested"],
                "tickers_tested":  result["tickers_tested"],
                "holding_periods": HOLDING_PERIODS,
                "status":          "success",
                "notes":           f"{result['result_rows']} result rows, {result['ticker_rows']} ticker rows",
            }, on_conflict="run_id").execute()
        except Exception as exc:
            logger.warning(f"  Could not write backtest_runs row: {exc}")

    # ── Print summary ─────────────────────────────────────────────────────────
    logger.info("\n" + "=" * 60)
    logger.info("  BACKTEST COMPLETE")
    logger.info("=" * 60)
    logger.info(f"  Periods evaluated : {result['periods_tested']}")
    logger.info(f"  Result rows       : {result['result_rows']}")
    logger.info(f"  Ticker rows       : {result['ticker_rows']}")
    logger.info(f"  Tickers tested    : {', '.join(result['tickers_tested'])}")

    if result.get("summary"):
        # Benchmark columns are None until a window matures — render those as N/A
        # rather than letting a %-format raise or a nan slip through.
        def _pct(val, width):
            return f"{val:>+{width}.2f}%" if val is not None else f"{'N/A':>{width + 1}}"

        summ = result["summary"]
        # Period rows are the keys like "1d"/"5d"/"20d"; other keys carry
        # portfolio-level metrics and must not be iterated as periods.
        period_rows = {k: v for k, v in summ.items()
                       if isinstance(v, dict) and "periods" in v}

        logger.info("\n  PERFORMANCE SUMMARY (gross / net of "
                    f"{summ.get('cost_bps_per_side', 0):.0f}bps/side):")
        logger.info(f"  {'Period':<8}  {'Portfolio':>10}  {'Net':>8}  {'SPY':>8}  "
                    f"{'Alpha':>8}  {'NetAlpha':>9}  {'Sharpe':>7}  {'Cov':>7}")
        logger.info(f"  {'─'*78}")
        for period, stats in period_rows.items():
            coverage = f"{stats.get('spy_periods', 0)}/{stats['periods']}"
            sharpe = stats.get("sharpe_like")
            sharpe_str = f"{sharpe:7.2f}" if sharpe is not None else f"{'N/A':>7}"
            logger.info(
                f"  {period:<8}  "
                f"{_pct(stats['avg_portfolio_return'], 9)}  "
                f"{_pct(stats.get('avg_net_return'), 7)}  "
                f"{_pct(stats['avg_spy_return'], 7)}  "
                f"{_pct(stats['avg_alpha_spy'], 7)}  "
                f"{_pct(stats.get('avg_net_alpha_spy'), 8)}  "
                f"{sharpe_str}  "
                f"{coverage:>7}"
            )

        dd = summ.get("max_drawdown_pct")
        if dd:
            logger.info(f"\n  Max drawdown (paper book, 1d-chained): "
                        f"{dd['max_drawdown_pct']:.2f}% over {dd['days']} days")

        by_strength = summ.get("hit_rate_by_strength") or {}
        if by_strength:
            logger.info("  Hit rate by signal strength (evaluated calls):")
            for strength in ("STRONG", "MODERATE", "WEAK", "UNKNOWN"):
                rec = by_strength.get(strength)
                if rec:
                    logger.info(f"    {strength:<9}: {rec['rate']*100:5.1f}% of {rec['n']} calls")

    if args.dry_run:
        logger.info("\n  [DRY RUN] No data written to Supabase.")
    else:
        logger.info("\n  Results saved. Query these views for analysis:")
        logger.info("    backtest_portfolio_summary  — aggregate performance by holding period")
        logger.info("    backtest_signal_accuracy    — per-ticker signal accuracy %")

    logger.info("=" * 60)


if __name__ == "__main__":
    main()