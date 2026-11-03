"""
backtest_engine.py — Orblex Industries Backtesting Engine

Evaluates all historical allocation plans against actual price outcomes.

Logic:
  For each allocation plan in allocation_details:
    For each holding period in [1, 5, 20] trading days:
      1. Get entry prices (close on plan_date)
      2. Get exit prices (close N trading days later)
      3. Compute weighted portfolio return using allocated_pct
      4. Compute SPY return over same window
      5. Compute equal-weight return across all tickers
      6. Compute alpha vs both benchmarks
      7. Evaluate signal correctness per ticker

Signal correctness rules:
  BUY  → correct if ticker_return_pct > 0 over the holding period
  SELL → not evaluated (excluded at 0%), marked as N/A
  HOLD → correct if abs(ticker_return_pct) < 3% (low volatility expected)
"""

import logging
from datetime import date, timedelta
from typing import Optional

import pandas as pd
from supabase import Client

logger = logging.getLogger(__name__)

HOLDING_PERIODS = [1, 5, 20]   # trading days
SPY_TICKER      = "SPY"

# HOLD correctness (2.4): the old rule "correct if |return| < 3%" rewarded HOLD on
# quiet names regardless of skill. Instead judge each HOLD against the name's OWN
# expected move over the horizon — 1 trailing-sigma — so a HOLD is "correct" only
# when the stock stayed inside its normal range, which scales with volatility.
HOLD_VOL_WINDOW   = 20     # trailing trading days for the vol estimate
HOLD_SIGMA        = 1.0    # within N sigma of a zero move counts as a good HOLD
HOLD_FALLBACK_PCT = 3.0    # used only when trailing vol cannot be computed

# Transaction-cost assumption for the net-of-cost summary (2.4). A held position
# is entered at plan_date and exited at the horizon — a round trip — so the cost
# on its weight is applied twice. Gross figures are unchanged; net is additive.
COST_BPS_PER_SIDE = 5.0    # 5 bps per side → 10 bps round trip


def _trailing_daily_vol(price_df, ticker: str, plan_date: date, window: int = HOLD_VOL_WINDOW):
    """
    Std of daily returns over the `window` trading days ending on/before plan_date,
    as a fraction (e.g. 0.02 = 2%/day). None if there is not enough history — the
    caller then falls back to the fixed band.
    """
    df = price_df[(price_df["ticker"] == ticker) & (price_df["date"] <= plan_date)]
    closes = df.sort_values("date")["close"].astype(float)
    if len(closes) < 6:
        return None
    rets = closes.pct_change().dropna().tail(window)
    if len(rets) < 5:
        return None
    vol = float(rets.std())
    return vol if vol > 0 else None


def _hold_is_correct(ticker_return_pct: float, daily_vol, n_days: int) -> bool:
    """
    A HOLD is correct if the realised move stayed within the name's expected
    1-sigma move over the horizon. Falls back to the fixed band when vol is
    unavailable so behaviour is defined for every ticker.
    """
    if daily_vol is None:
        return abs(ticker_return_pct) < HOLD_FALLBACK_PCT
    expected_move_pct = daily_vol * (n_days ** 0.5) * 100
    return abs(ticker_return_pct) <= HOLD_SIGMA * expected_move_pct


# ── Price fetchers ────────────────────────────────────────────────────────────

def _fetch_prices_for_tickers(
    client: Client,
    tickers: list[str],
    from_date: str,
    to_date: str,
) -> pd.DataFrame:
    """
    Fetches all stock_prices rows for given tickers between from_date and to_date.
    Returns DataFrame with columns: ticker, date, close.
    """
    try:
        resp = (
            client.table("stock_prices")
            .select("ticker,date,close")
            .in_("ticker", tickers)
            .gte("date", from_date)
            .lte("date", to_date)
            .order("date", desc=False)
            .execute()
        )
        if not resp.data:
            return pd.DataFrame(columns=["ticker", "date", "close"])
        df = pd.DataFrame(resp.data)
        df["date"]  = pd.to_datetime(df["date"]).dt.date
        df["close"] = df["close"].astype(float)
        return df
    except Exception as exc:
        logger.error(f"  Price fetch failed: {exc}")
        return pd.DataFrame(columns=["ticker", "date", "close"])


def _get_close_on_or_after(
    price_df: pd.DataFrame,
    ticker: str,
    target_date: date,
) -> Optional[tuple[float, date]]:
    """
    Returns (close_price, actual_date) on or after target_date for ticker.
    Uses next available trading day if target_date is a holiday/weekend.
    Returns None if no price found within 5 calendar days.
    """
    df = price_df[price_df["ticker"] == ticker].copy()
    if df.empty:
        return None

    for offset in range(5):
        check_date = target_date + timedelta(days=offset)
        row = df[df["date"] == check_date]
        if not row.empty:
            return float(row.iloc[0]["close"]), check_date

    return None


def _get_nth_trading_day(
    price_df: pd.DataFrame,
    ticker: str,
    from_date: date,
    n: int,
) -> Optional[tuple[float, date]]:
    """
    Returns (close_price, date) for the Nth trading day after from_date.
    Uses the sorted unique trading dates available in the price data.
    """
    df = price_df[price_df["ticker"] == ticker].copy()
    df = df[df["date"] > from_date].sort_values("date")

    if len(df) < n:
        return None

    row = df.iloc[n - 1]
    return float(row["close"]), row["date"]


# ── Benchmark fetchers ────────────────────────────────────────────────────────

def _dates_in_range(client: Client, tickers: list[str], from_date: str, to_date: str) -> set[str]:
    """Distinct stock_prices dates (as ISO strings) present for `tickers` in range."""
    try:
        resp = (
            client.table("stock_prices")
            .select("date")
            .in_("ticker", tickers)
            .gte("date", from_date)
            .lte("date", to_date)
            .execute()
        )
        return {str(r["date"]) for r in (resp.data or [])}
    except Exception as exc:
        logger.warning(f"  Date-range lookup failed for {tickers[:3]}...: {exc}")
        return set()


def _ensure_spy_prices(
    client: Client,
    from_date: str,
    to_date: str,
    reference_tickers: Optional[list[str]] = None,
    dry_run: bool = False,
) -> bool:
    """
    Ensure SPY prices cover the trading days actually present for the portfolio
    over [from_date, to_date], and backfill the gap when they don't.

    The old logic returned as soon as ONE SPY row existed in the range, so a
    partially-covered range never got filled — which is why 20-day SPY alpha read
    N/A for every plan (IMPROVEMENTS.md 2.4). Now the reference trading calendar is
    the set of dates present for the portfolio tickers; if SPY is missing more than
    a day of that calendar, fetch the full range and upsert the missing dates.
    Returns True if SPY ends up adequately covered.
    """
    from data_provider import get_provider, YFinanceProvider
    from datetime import datetime as _dt

    try:
        spy_dates = _dates_in_range(client, [SPY_TICKER], from_date, to_date)
        ref_dates = (
            _dates_in_range(client, reference_tickers, from_date, to_date)
            if reference_tickers else set()
        )

        # Without a reference calendar we can only tell present-vs-absent.
        missing = (ref_dates - spy_dates) if ref_dates else set()
        if ref_dates:
            if len(missing) <= 1:
                logger.info(
                    f"  SPY coverage OK: {len(spy_dates)}/{len(ref_dates)} reference days"
                )
                return True
            logger.info(
                f"  SPY coverage gap: missing {len(missing)} of {len(ref_dates)} "
                f"reference trading day(s) — backfilling"
            )
        elif spy_dates:
            return True   # can't judge a gap, but SPY is not empty

        if dry_run:
            logger.info(
                f"  [DRY RUN] Would fetch SPY to backfill "
                f"{len(missing) or 'all'} missing day(s) in {from_date}→{to_date}"
            )
            return bool(spy_dates)

        days = (_dt.fromisoformat(to_date) - _dt.fromisoformat(from_date)).days + 5
        provider = get_provider()
        logger.info(f"  Fetching SPY prices ({from_date} → {to_date}) via {provider.name}...")
        df = provider.fetch_prices(SPY_TICKER, days=days)

        if df.empty and provider.name != "yfinance":
            logger.warning(f"  {provider.name} returned no SPY data — falling back to yfinance")
            df = YFinanceProvider().fetch_prices(SPY_TICKER, days=days)

        if df.empty:
            logger.warning("  Could not fetch SPY prices")
            return bool(spy_dates)

        # Filter to requested date range, then to the dates SPY is actually missing
        # (when we have a reference) so we upsert only what is needed.
        df = df[(df["date"].astype(str) >= from_date) & (df["date"].astype(str) <= to_date)]
        if missing:
            df = df[df["date"].astype(str).isin(missing)]

        rows = [
            {
                "ticker":    SPY_TICKER,
                "date":      str(row["date"]),
                "open":      float(row["open"]),
                "high":      float(row["high"]),
                "low":       float(row["low"]),
                "close":     float(row["close"]),
                "adj_close": float(row["adj_close"]),
                "volume":    int(row["volume"]),
            }
            for _, row in df.iterrows()
        ]

        if rows:
            client.table("stock_prices").upsert(rows, on_conflict="ticker,date").execute()
            logger.info(f"  SPY: {len(rows)} row(s) upserted via {provider.name}")
        return True

    except Exception as exc:
        logger.warning(f"  SPY price fetch failed: {exc}")
        return False


# ── Core backtest logic ───────────────────────────────────────────────────────

def _evaluate_plan(
    plan: dict,
    details: list[dict],
    price_df: pd.DataFrame,
    backtest_run_id: str,
    env_tickers: set[str] | None = None,
) -> list[dict]:
    """
    Evaluates one allocation plan across all holding periods.
    Returns list of backtest_results rows (one per holding period).
    """
    plan_date    = pd.to_datetime(plan["plan_date"]).date()
    alloc_run_id = plan["run_id"]
    cash_pct     = float(plan.get("cash_pct") or 0.0)
    invested_pct = float(plan.get("invested_pct") or 0.0)
    risk_level   = plan.get("market_risk_level", "UNKNOWN")

    results = []

    for n_days in HOLDING_PERIODS:
        ticker_rows    = []
        portfolio_gain = 0.0
        eq_weight_gain = 0.0
        data_complete  = True
        eq_weight_n    = 0

        # ── Per-ticker returns ─────────────────────────────────────────────
        for d in [d for d in details if not env_tickers or d["ticker"] in env_tickers]:
            ticker       = d["ticker"]
            alloc_pct    = float(d.get("allocated_pct") or 0.0)
            signal       = d.get("analyst_signal", "HOLD")
            strength     = d.get("signal_strength", "WEAK")

            entry = _get_close_on_or_after(price_df, ticker, plan_date)
            exit_ = _get_nth_trading_day(price_df, ticker, plan_date, n_days)

            if entry is None or exit_ is None:
                logger.warning(f"    Missing price data: {ticker} on {plan_date} +{n_days}d")
                data_complete = False
                continue

            entry_price, _          = entry
            exit_price,  exit_date  = exit_

            ticker_return = ((exit_price - entry_price) / entry_price) * 100
            weighted      = ticker_return * (alloc_pct / 100)
            portfolio_gain += weighted

            # Equal-weight contribution (all tickers, same weight)
            eq_weight_gain += ticker_return
            eq_weight_n    += 1

            # Signal correctness
            if signal == "SELL" or alloc_pct == 0:
                signal_correct = None   # excluded — not evaluated
            elif signal == "BUY":
                signal_correct = ticker_return > 0
            else:  # HOLD — correct if it stayed within its own expected move
                daily_vol = _trailing_daily_vol(price_df, ticker, plan_date)
                signal_correct = _hold_is_correct(ticker_return, daily_vol, n_days)

            ticker_rows.append({
                "backtest_run_id":       backtest_run_id,
                "alloc_run_id":          alloc_run_id,
                "plan_date":             plan_date.isoformat(),
                "ticker":                ticker,
                "holding_period_days":   n_days,
                "allocated_pct":         alloc_pct,
                "analyst_signal":        signal,
                "signal_strength":       strength,
                "entry_price":           round(entry_price, 4),
                "exit_price":            round(exit_price, 4),
                "exit_date":             exit_date.isoformat(),
                "ticker_return_pct":     round(ticker_return, 4),
                "weighted_contribution": round(weighted, 4),
                "signal_correct":        signal_correct,
            })

        # ── SPY return ─────────────────────────────────────────────────────
        spy_entry = _get_close_on_or_after(price_df, SPY_TICKER, plan_date)
        spy_exit  = _get_nth_trading_day(price_df, SPY_TICKER, plan_date, n_days)
        spy_return = None
        exit_date  = None

        if spy_entry and spy_exit:
            spy_return = ((spy_exit[0] - spy_entry[0]) / spy_entry[0]) * 100
            exit_date  = spy_exit[1]

        # ── Equal-weight return ────────────────────────────────────────────
        eq_return = (eq_weight_gain / eq_weight_n) if eq_weight_n > 0 else None

        # ── Portfolio total return (positions + cash drag) ─────────────────
        # Cash earns 0% — it's a drag vs benchmarks during bull markets
        total_return = round(portfolio_gain, 4)  # cash contributes 0

        alpha_spy = (
            round(total_return - spy_return, 4)
            if spy_return is not None else None
        )
        alpha_eq = (
            round(total_return - eq_return, 4)
            if eq_return is not None else None
        )

        result_row = {
            "backtest_run_id":       backtest_run_id,
            "alloc_run_id":          alloc_run_id,
            "plan_date":             plan_date.isoformat(),
            "holding_period_days":   n_days,
            "exit_date":             exit_date.isoformat() if exit_date else None,
            "portfolio_return_pct":  round(portfolio_gain, 4),
            "cash_return_pct":       0.0,
            "total_return_pct":      total_return,
            "spy_return_pct":        round(spy_return, 4) if spy_return is not None else None,
            "eq_weight_return_pct":  round(eq_return, 4)  if eq_return  is not None else None,
            "alpha_vs_spy":          alpha_spy,
            "alpha_vs_eq_weight":    alpha_eq,
            "market_risk_level":     risk_level,
            "cash_pct":              cash_pct,
            "invested_pct":          invested_pct,
            "tickers_evaluated":     len(ticker_rows),
            "data_complete":         data_complete,
        }

        results.append({
            "result_row":  result_row,
            "ticker_rows": ticker_rows,
        })

        logger.info(
            f"    {plan_date} +{n_days:2d}d  "
            f"portfolio={total_return:+.2f}%  "
            f"spy={spy_return:+.2f}%  " if spy_return is not None else
            f"    {plan_date} +{n_days:2d}d  "
            f"portfolio={total_return:+.2f}%  spy=N/A  "
            f"alpha={alpha_spy:+.2f}%  " if alpha_spy is not None else ""
        )

    return results


# ── Risk-adjusted summary helpers (2.4) ────────────────────────────────────────

def _max_drawdown_1d(result_rows: list[dict]) -> Optional[dict]:
    """
    Worst peak-to-trough decline of the paper book, from the 1-day forward returns
    chained in plan_date order. A proxy (no live positions yet — Part 4) but built
    from realised returns. None when there is too little history.
    """
    rows = sorted(
        (r for r in result_rows
         if r.get("holding_period_days") == 1 and r.get("total_return_pct") is not None),
        key=lambda r: r["plan_date"],
    )
    if len(rows) < 3:
        return None
    equity = peak = 1.0
    max_dd = 0.0
    for r in rows:
        equity *= (1 + float(r["total_return_pct"]) / 100)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1)
    return {"max_drawdown_pct": round(max_dd * 100, 2), "days": len(rows)}


def _hit_rate_by_strength(ticker_rows: list[dict]) -> dict:
    """
    Hit rate grouped by the analyst's signal_strength, over evaluated outcomes
    (signal_correct is None for SELL/excluded and is skipped). Exposes whether a
    STRONG call is actually more reliable than a WEAK one — the metric the old
    aggregate hid.
    """
    acc: dict = {}
    for r in ticker_rows:
        correct = r.get("signal_correct")
        if correct is None:
            continue
        strength = (r.get("signal_strength") or "UNKNOWN").upper()
        slot = acc.setdefault(strength, [0, 0])
        slot[0] += 1 if correct else 0
        slot[1] += 1
    return {
        s: {"rate": round(c / t, 3), "n": t}
        for s, (c, t) in acc.items() if t > 0
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def run_backtest(
    client: Client,
    backtest_run_id: str,
    tickers: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Runs backtesting across all historical allocation plans.
    tickers: current portfolio tickers from .env — seeded into the price fetch
             so data is always available even for tickers not yet in old plans.
    Returns summary dict with aggregate statistics.
    """
    logger.info("\n  [1/5] Fetching all allocation plans...")
    plans_resp = (
        client.table("allocation_plans")
        .select("*")
        .order("plan_date", desc=False)
        .execute()
    )
    plans = plans_resp.data or []
    logger.info(f"  Found {len(plans)} allocation plan(s)")

    if not plans:
        return {"error": "No allocation plans found. Run run_allocator.py first."}

    # ── Gather all tickers + date range ───────────────────────────────────────
    logger.info("\n  [2/5] Gathering tickers and date range...")
    env_set     = set(tickers or [])
    all_tickers = set([SPY_TICKER] + (tickers or []))
    all_details = {}  # alloc_run_id → list of detail dicts

    for plan in plans:
        run_id = plan["run_id"]
        resp   = (
            client.table("allocation_details")
            .select("*")
            .eq("plan_run_id", run_id)
            .execute()
        )
        details = resp.data or []
        all_details[run_id] = details
        # Only include tickers present in the current .env list; historical
        # tickers from old runs (NBIS, MDB, etc.) are intentionally excluded.
        for d in details:
            if not env_set or d["ticker"] in env_set:
                all_tickers.add(d["ticker"])

    # Date range: earliest plan_date to today + 30 buffer for exit prices
    earliest = min(p["plan_date"] for p in plans)
    latest   = date.today().isoformat()
    logger.info(f"  Tickers (.env) : {sorted(env_set)}")
    logger.info(f"  Date range     : {earliest} → {latest}")

    # ── Ensure SPY prices exist ────────────────────────────────────────────────
    logger.info("\n  [3/5] Ensuring SPY benchmark prices...")
    # Use the portfolio tickers as the reference trading calendar so partial SPY
    # coverage is backfilled, not silently skipped.
    reference = sorted(all_tickers - {SPY_TICKER}) or (tickers or [])
    _ensure_spy_prices(client, earliest, latest, reference_tickers=reference, dry_run=dry_run)

    # ── Bulk fetch all prices ──────────────────────────────────────────────────
    logger.info("\n  [4/5] Loading price data...")
    price_df = _fetch_prices_for_tickers(client, list(all_tickers), earliest, latest)
    logger.info(f"  Loaded {len(price_df)} price rows across {price_df['ticker'].nunique()} tickers")

    # ── Evaluate each plan ────────────────────────────────────────────────────
    logger.info("\n  [5/5] Running backtest evaluations...")
    all_result_rows  = []
    all_ticker_rows  = []
    periods_tested   = 0

    for plan in plans:
        run_id  = plan["run_id"]
        details = all_details.get(run_id, [])

        if not details:
            logger.warning(f"  No allocation details for {run_id} — skipping")
            continue

        logger.info(f"\n  Plan: {plan['plan_date']} ({run_id[:30]}...)")
        plan_results = _evaluate_plan(plan, details, price_df, backtest_run_id, env_tickers=env_set or None)

        for pr in plan_results:
            all_result_rows.append(pr["result_row"])
            all_ticker_rows.extend(pr["ticker_rows"])

        periods_tested += 1

    # ── Save results ───────────────────────────────────────────────────────────
    if dry_run:
        logger.info(f"\n  [DRY RUN] Would write {len(all_result_rows)} result rows")
        logger.info(f"  [DRY RUN] Would write {len(all_ticker_rows)} ticker result rows")
    else:
        if all_result_rows:
            client.table("backtest_results").upsert(
                all_result_rows, on_conflict="alloc_run_id,holding_period_days"
            ).execute()
            logger.info(f"\n  Saved {len(all_result_rows)} backtest_results rows")

        if all_ticker_rows:
            client.table("backtest_ticker_results").upsert(
                all_ticker_rows, on_conflict="alloc_run_id,ticker,holding_period_days"
            ).execute()
            logger.info(f"  Saved {len(all_ticker_rows)} backtest_ticker_results rows")

    # ── Compute summary stats ──────────────────────────────────────────────────
    # A recent plan has no matured 5d/20d exit yet, so its benchmark columns are
    # None. Averaging an all-None column yields nan; _safe_mean returns None
    # instead so nothing downstream (prompts, newsletter) has to filter nan out.
    def _safe_mean(series) -> Optional[float]:
        clean = series.dropna()
        return round(clean.mean(), 4) if not clean.empty else None

    def _sharpe(series) -> Optional[float]:
        """Return/volatility of the per-plan returns (not annualised, so it is not
        overstated). None when there are too few points or zero dispersion."""
        clean = series.dropna()
        if len(clean) < 2:
            return None
        sd = float(clean.std())
        return round(float(clean.mean()) / sd, 4) if sd > 0 else None

    if all_result_rows:
        df = pd.DataFrame(all_result_rows)
        # Net-of-cost return: a held position is a round trip, so apply the per-side
        # cost twice to the invested fraction. Gross columns are untouched.
        roundtrip = 2 * COST_BPS_PER_SIDE / 1e4
        df["cost_pct"] = df["invested_pct"].fillna(0.0) * roundtrip
        df["net_return_pct"] = df["total_return_pct"] - df["cost_pct"]
        df["net_alpha_vs_spy"] = df["alpha_vs_spy"] - df["cost_pct"]

        summary = {}
        for n in HOLDING_PERIODS:
            sub = df[df["holding_period_days"] == n]
            if sub.empty:
                continue
            summary[f"{n}d"] = {
                "avg_portfolio_return": _safe_mean(sub["total_return_pct"]),
                "avg_net_return":       _safe_mean(sub["net_return_pct"]),
                "avg_spy_return":       _safe_mean(sub["spy_return_pct"]),
                "avg_eq_return":        _safe_mean(sub["eq_weight_return_pct"]),
                "avg_alpha_spy":        _safe_mean(sub["alpha_vs_spy"]),
                "avg_net_alpha_spy":    _safe_mean(sub["net_alpha_vs_spy"]),
                "sharpe_like":          _sharpe(sub["total_return_pct"]),
                "periods":              len(sub),
                # How many of those periods actually had a matured SPY benchmark.
                # A gap between this and `periods` means the window hasn't matured
                # (or SPY data is missing), not that alpha is genuinely zero.
                "spy_periods":          int(sub["spy_return_pct"].notna().sum()),
            }

        summary["max_drawdown_pct"]     = _max_drawdown_1d(all_result_rows)
        summary["hit_rate_by_strength"] = _hit_rate_by_strength(all_ticker_rows)
        summary["cost_bps_per_side"]    = COST_BPS_PER_SIDE
    else:
        summary = {}

    return {
        "periods_tested":  periods_tested,
        "result_rows":     len(all_result_rows),
        "ticker_rows":     len(all_ticker_rows),
        "tickers_tested":  sorted(all_tickers - {SPY_TICKER}),
        "summary":         summary,
    }