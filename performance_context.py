"""
performance_context.py — reads the backtest feedback tables back into a compact
performance summary, and formats it for the analyst and risk prompts.

This is the read side of the feedback loop (IMPROVEMENTS.md item 2.1). Stage 9
(`run_backtest.py`) writes `backtest_results` (portfolio-level, per plan per
holding period) and `backtest_ticker_results` (per-ticker signal correctness).
Nothing read those tables until now — the system measured whether it was right
and then discarded the answer. This module surfaces three things the next run's
prompts can react to:

  - per-ticker hit rate at 5d / 20d      (was a BUY/HOLD actually correct?)
  - portfolio alpha vs SPY at 5d / 20d   (did the book beat the benchmark?)
  - current drawdown of the paper book   (how deep underwater are we now?)

Everything degrades to "no data yet" rather than raising: a fresh system with no
matured windows, or a backtest that has not run, must not break the daily prompt.
Per IMPROVEMENTS.md 2.4, an absent alpha means "no benchmark matured yet" and is
never treated as zero.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Holding periods we surface in prompts. 1d is used only for the drawdown curve.
FEEDBACK_PERIODS = [5, 20]
DRAWDOWN_PERIOD  = 1

# Don't feed a hit rate the model will over-trust: below this many evaluated
# outcomes the sample is noise, so we label it low-confidence.
MIN_HITRATE_SAMPLE = 5


def _latest_backtest_run_id(client) -> Optional[str]:
    """Most recent successful backtest run, or None if none has run."""
    try:
        resp = (
            client.table("backtest_runs")
            .select("run_id,run_date,status")
            .eq("status", "success")
            .order("run_date", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0]["run_id"] if resp.data else None
    except Exception as exc:
        logger.warning(f"  Could not read backtest_runs: {exc}")
        return None


def _fetch_ticker_results(client, backtest_run_id: str) -> list[dict]:
    try:
        resp = (
            client.table("backtest_ticker_results")
            .select("ticker,holding_period_days,analyst_signal,signal_correct")
            .eq("backtest_run_id", backtest_run_id)
            .execute()
        )
        return resp.data or []
    except Exception as exc:
        logger.warning(f"  Could not read backtest_ticker_results: {exc}")
        return []


def _fetch_portfolio_results(client, backtest_run_id: str) -> list[dict]:
    try:
        resp = (
            client.table("backtest_results")
            .select("plan_date,holding_period_days,total_return_pct,alpha_vs_spy,spy_return_pct")
            .eq("backtest_run_id", backtest_run_id)
            .order("plan_date", desc=False)
            .execute()
        )
        return resp.data or []
    except Exception as exc:
        logger.warning(f"  Could not read backtest_results: {exc}")
        return []


def _hit_rates(ticker_rows: list[dict]) -> dict:
    """
    {ticker: {5: {"rate": 0.62, "n": 8}, 20: {...}}} for evaluated outcomes only.
    signal_correct is None for SELL / excluded names, which are not scored.
    """
    # ticker -> period -> [correct_count, evaluated_count]
    acc: dict = {}
    for r in ticker_rows:
        correct = r.get("signal_correct")
        if correct is None:
            continue
        period = r.get("holding_period_days")
        if period not in FEEDBACK_PERIODS:
            continue
        t = r["ticker"]
        slot = acc.setdefault(t, {}).setdefault(period, [0, 0])
        slot[0] += 1 if correct else 0
        slot[1] += 1

    out: dict = {}
    for t, periods in acc.items():
        out[t] = {}
        for period, (correct, total) in periods.items():
            if total > 0:
                out[t][period] = {"rate": round(correct / total, 3), "n": total}
    return out


def _overall_hit_rate(ticker_rows: list[dict]) -> dict:
    """
    Portfolio-wide hit rate per period, pooled across every scored ticker outcome
    (not an average of per-ticker rates, so a name with more calls weighs more).
    {5: {"rate": 0.61, "n": 84}, 20: {...}}, periods with no scored outcomes omitted.
    Computed from the raw rows so it is exact, not reconstructed from rounded rates.
    """
    acc: dict = {p: [0, 0] for p in FEEDBACK_PERIODS}  # period -> [correct, total]
    for r in ticker_rows:
        correct = r.get("signal_correct")
        if correct is None:
            continue
        period = r.get("holding_period_days")
        if period not in FEEDBACK_PERIODS:
            continue
        acc[period][0] += 1 if correct else 0
        acc[period][1] += 1

    out: dict = {}
    for period, (correct, total) in acc.items():
        if total > 0:
            out[period] = {"rate": round(correct / total, 3), "n": total}
    return out


def _mean(values: list[float]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 3) if clean else None


def _portfolio_alpha(portfolio_rows: list[dict]) -> dict:
    """Average alpha vs SPY per period over plans whose window has matured."""
    out: dict = {}
    for period in FEEDBACK_PERIODS:
        rows = [r for r in portfolio_rows if r.get("holding_period_days") == period]
        alphas = [r.get("alpha_vs_spy") for r in rows]
        matured = [a for a in alphas if a is not None]
        out[period] = {
            "avg_alpha_spy": _mean(alphas),   # None when nothing has matured
            "matured": len(matured),
            "plans": len(rows),
        }
    return out


def _current_drawdown(portfolio_rows: list[dict]) -> Optional[dict]:
    """
    Approximate the paper book's equity curve by chaining each plan's realised
    1-day forward return in plan_date order, then report how far below the peak
    the curve currently sits. This is a proxy — the system has no live positions
    table yet (that is Part 4) — but it is grounded in realised returns, not a
    model guess.
    """
    rows = sorted(
        (r for r in portfolio_rows
         if r.get("holding_period_days") == DRAWDOWN_PERIOD
         and r.get("total_return_pct") is not None),
        key=lambda r: r["plan_date"],
    )
    if len(rows) < 3:
        return None

    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in rows:
        equity *= (1 + float(r["total_return_pct"]) / 100)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1)

    current_dd = equity / peak - 1
    return {
        "current_drawdown_pct": round(current_dd * 100, 2),
        "max_drawdown_pct":     round(max_dd * 100, 2),
        "days": len(rows),
    }


def build_performance_summary(client, tickers: Optional[list[str]] = None) -> Optional[dict]:
    """
    Reads the latest backtest run into a compact summary, or None if no backtest
    has run yet. `tickers`, when given, limits per-ticker hit rates to the current
    universe (the backtest also scores names that have since left the portfolio).
    """
    backtest_run_id = _latest_backtest_run_id(client)
    if not backtest_run_id:
        logger.info("  No completed backtest run — performance feedback unavailable.")
        return None

    ticker_rows    = _fetch_ticker_results(client, backtest_run_id)
    portfolio_rows = _fetch_portfolio_results(client, backtest_run_id)
    if not ticker_rows and not portfolio_rows:
        logger.info(f"  Backtest run {backtest_run_id} has no rows — feedback unavailable.")
        return None

    hit_rates = _hit_rates(ticker_rows)
    if tickers:
        universe = {t.upper() for t in tickers}
        hit_rates = {t: v for t, v in hit_rates.items() if t.upper() in universe}

    return {
        "backtest_run_id":  backtest_run_id,
        "hit_rates":        hit_rates,
        # Portfolio-wide pooled hit rate, over the FULL scored universe — not
        # filtered by `tickers`, so the scorecard reflects the whole track record
        # even when the current book is a subset.
        "hit_rate_overall": _overall_hit_rate(ticker_rows),
        "portfolio_alpha":  _portfolio_alpha(portfolio_rows),
        "drawdown":         _current_drawdown(portfolio_rows),
    }


# ── Prompt formatting ───────────────────────────────────────────────────────

def _fmt_pct(val: Optional[float], signed: bool = True) -> str:
    if val is None:
        return "N/A"
    return f"{val:+.2f}%" if signed else f"{val:.2f}%"


def _portfolio_lines(summary: dict) -> list[str]:
    """Portfolio-wide alpha + drawdown lines shared by both prompts."""
    lines = []
    alpha = summary.get("portfolio_alpha") or {}
    for period in FEEDBACK_PERIODS:
        a = alpha.get(period) or {}
        avg = a.get("avg_alpha_spy")
        if avg is None:
            lines.append(f"  Alpha vs SPY ({period}d): N/A — no window matured yet "
                         f"({a.get('matured', 0)}/{a.get('plans', 0)} plans)")
        else:
            lines.append(f"  Alpha vs SPY ({period}d): {avg:+.2f}% avg over "
                         f"{a.get('matured', 0)} matured plan(s)")

    dd = summary.get("drawdown")
    if dd:
        lines.append(f"  Paper book drawdown: {dd['current_drawdown_pct']:.2f}% now "
                     f"(worst {dd['max_drawdown_pct']:.2f}% over {dd['days']} days)")
    else:
        lines.append("  Paper book drawdown: N/A — insufficient history")
    return lines


def format_analyst_feedback(summary: Optional[dict], ticker: str) -> str:
    """
    Compact block for the single-ticker analyst prompt: this ticker's own recent
    hit rate, plus portfolio-level alpha/drawdown for context.
    """
    if not summary:
        return "  No backtest feedback available yet (system still accumulating history)."

    lines = []
    tk = summary.get("hit_rates", {}).get(ticker.upper(), {})
    for period in FEEDBACK_PERIODS:
        rec = tk.get(period)
        if not rec:
            lines.append(f"  {ticker} signal hit rate ({period}d): N/A — no scored outcomes yet")
        else:
            conf = "" if rec["n"] >= MIN_HITRATE_SAMPLE else "  (low sample — treat as weak evidence)"
            lines.append(f"  {ticker} signal hit rate ({period}d): "
                         f"{rec['rate'] * 100:.0f}% of {rec['n']} calls correct{conf}")

    lines.extend(_portfolio_lines(summary))
    return "\n".join(lines)


def format_risk_feedback(summary: Optional[dict], top_n: int = 5) -> str:
    """
    Compact block for the portfolio-wide risk prompt: alpha/drawdown, plus the
    chronic wrong-callers (lowest 20d hit rate with a usable sample) so the Risk
    Manager can lean harder on names the analyst has been repeatedly wrong about.
    """
    if not summary:
        return "  No backtest feedback available yet (system still accumulating history)."

    lines = list(_portfolio_lines(summary))

    # Worst performers by 20d hit rate, restricted to a meaningful sample.
    scored = []
    for t, periods in summary.get("hit_rates", {}).items():
        rec = periods.get(20) or periods.get(5)
        if rec and rec["n"] >= MIN_HITRATE_SAMPLE:
            scored.append((t, rec["rate"], rec["n"]))
    scored.sort(key=lambda x: x[1])

    if scored:
        worst = scored[:top_n]
        lines.append("  Lowest signal hit rates (candidates for tighter caps):")
        for t, rate, n in worst:
            lines.append(f"    {t}: {rate * 100:.0f}% correct over {n} calls")
    else:
        lines.append("  Per-ticker hit rates: not enough scored history to rank yet")

    return "\n".join(lines)


# ── Swing mode (TRADING_MODE=swing) ─────────────────────────────────────────

def build_swing_feedback(client) -> Optional[dict]:
    """
    Closed-trade statistics from swing_backtest_trades (written by Stage 9),
    via swing_backtest.summarize. None when the table is missing or empty —
    the swing prompt then says no history yet.
    """
    from swing_backtest import summarize
    try:
        resp = (
            client.table("swing_backtest_trades")
            .select("ticker,outcome,fill_date,r_multiple,return_pct,hold_sessions,"
                    "setup_type,signal_strength")
            .execute()
        )
    except Exception as exc:
        logger.info(f"  Swing backtest feedback unavailable: {exc}")
        return None
    return summarize(resp.data) if resp.data else None


def _swing_line(label: str, st: Optional[dict]) -> str:
    if not st:
        return f"  {label}: no closed trades yet"
    conf = "" if st["n"] >= MIN_HITRATE_SAMPLE else "  (low sample — weak evidence)"
    return (f"  {label}: {st['n']} closed, win {st['win_rate'] * 100:.0f}%, "
            f"expectancy {st['avg_r']:+.2f}R, avg hold {st['avg_hold']} sessions{conf}")


def format_swing_feedback(summary: Optional[dict], ticker: str) -> str:
    """Swing analyst feedback: this ticker's and the book's closed trades, in R."""
    if not summary or not summary.get("overall"):
        return "  No closed swing trades yet (system still accumulating history)."
    lines = [
        _swing_line(f"{ticker} swing trades", (summary.get("by_ticker") or {}).get(ticker)),
        _swing_line("All swing trades", summary["overall"]),
    ]
    setups = summary.get("by_setup") or {}
    if setups:
        lines.append("  By setup: " + ", ".join(
            f"{k} {v['avg_r']:+.2f}R (n={v['n']})" for k, v in setups.items()))
    if summary.get("fill_rate") is not None:
        lines.append(f"  Entry fill rate: {summary['fill_rate'] * 100:.0f}% of plans")
    return "\n".join(lines)
