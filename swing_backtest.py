"""
swing_backtest.py — scores swing-mode trade plans (TRADING_MODE=swing) by
simulating them on daily bars, the way the account would actually trade them.

The allocation backtest (backtest_engine.py) asks "was the 5/20-day close above
the plan-day close?". That is the wrong question for a swing plan with an entry
limit, a stop and a target. Here each swing BUY in agent_reports becomes one
simulated trade:

  Entry   A limit at entry_price, live for ENTRY_VALID_SESSIONS sessions from
          the report date (the plan is built pre-open from prior closes). An
          open at/below the limit fills at the open; otherwise a low that
          touches it fills at the limit. If the session opens at/below the stop,
          the setup is void and the order is cancelled.
  Hold    No exit on the fill day, so old and new plans stay comparable. The account
          may now exit that day (exec_limits.ALLOW_SAME_DAY_EXIT): fill_day_stop=True
          scores that, counting a fill-day low at or below the stop as stopped out,
          since daily bars cannot show whether the fill came first.
  Exit   Checked from the session after the fill, in this order:
            SIGNAL_EXIT  a later swing SELL for the ticker → exit at the open
            STOP         open at/below the stop → open (gap); low touches → stop
            TARGET       open at/above target → open; high touches → target
            TIME         horizon_days sessions after the fill → that close
          A bar that touches both stop and target counts as STOP (daily bars
          cannot tell which came first, so assume the worse).
  Book    One trade per ticker at a time: a BUY while a trade (or a resting
          entry) is live is SKIPPED_IN_TRADE, and an entry cannot fill on the
          session the previous trade exited (no same-day re-buy).

Returns are net of COST_BPS_PER_SIDE on each side and expressed both in % and
in R (multiples of the PLANNED risk, entry − stop — the unit the account sizes
from, so a fill that gaps to just above the stop does not inflate R). Trades
still running are OPEN / PENDING and are kept out of the statistics.
"""

import logging
from datetime import date, datetime, timezone
from typing import Optional

import pandas as pd
from supabase import Client

from backtest_engine import COST_BPS_PER_SIDE
from swing_policy import ENTRY_VALID_SESSIONS

logger = logging.getLogger(__name__)

# Outcomes. CLOSED_OUTCOMES feed the statistics; the rest are bookkeeping.
TARGET, STOP, TIME, SIGNAL_EXIT = "TARGET", "STOP", "TIME", "SIGNAL_EXIT"
NO_FILL, CANCELLED_GAP, SKIPPED = "NO_FILL", "CANCELLED_GAP", "SKIPPED_IN_TRADE"
PENDING, OPEN = "PENDING", "OPEN"
CLOSED_OUTCOMES = (TARGET, STOP, TIME, SIGNAL_EXIT)

# Below this many closed trades a rate is labelled low-confidence in prompts.
MIN_TRADE_SAMPLE = 5


def simulate_trade(plan: dict, bars: pd.DataFrame,
                   earliest_entry: Optional[date] = None,
                   sell_dates: Optional[list[date]] = None, fill_day_stop: bool = False) -> dict:
    """
    Simulate one plan on `bars` (one ticker, date-ascending, columns date/open/
    high/low/close with date as datetime.date). earliest_entry pushes the entry
    window back (the previous trade's exit session + 1); sell_dates are report
    dates of later swing SELLs on this ticker. Pure function — no I/O.
    """
    report_date = plan["report_date"]
    entry, stop, target = float(plan["entry_price"]), float(plan["stop_price"]), float(plan["target_price"])
    horizon = int(plan["horizon_days"])
    out = {"outcome": PENDING, "fill_date": None, "fill_price": None, "exit_date": None,
           "exit_price": None, "hold_sessions": None, "return_pct": None, "r_multiple": None,
           "mfe_r": None, "mae_r": None}

    window_start = max(report_date, earliest_entry) if earliest_entry else report_date
    future = bars[bars["date"] >= report_date].reset_index(drop=True)

    # ── Entry ───────────────────────────────────────────────────────────────
    entry_window = future[future["date"] >= window_start].head(ENTRY_VALID_SESSIONS)
    fill_idx = None
    for _, b in entry_window.iterrows():
        if b["open"] <= stop:
            out["outcome"] = CANCELLED_GAP
            return out
        if b["open"] <= entry:
            fill_idx, fill_price = b.name, float(b["open"])
            break
        if b["low"] <= entry:
            fill_idx, fill_price = b.name, entry
            break
    if fill_idx is None:
        # Only a full, unfilled window is a real NO_FILL; a short one is still live.
        if len(entry_window) == ENTRY_VALID_SESSIONS:
            out["outcome"] = NO_FILL
        return out

    fill_date = future.loc[fill_idx, "date"]
    # R is measured against the planned risk, not fill − stop: the account sizes
    # qty = risk $ / (entry − stop), and a gap fill just above the stop would
    # otherwise turn a few cents of P&L into ±60R.
    risk = entry - stop
    out.update(fill_date=fill_date, fill_price=round(fill_price, 4))
    signal_exit_on = min((d for d in (sell_dates or []) if d > fill_date), default=None)

    # ── Exit (from the next session, unless fill_day_stop) ─────────────────
    after = future.iloc[fill_idx + 1: fill_idx + 1 + horizon]
    exit_price = exit_date = outcome = None
    hi, lo = fill_price, fill_price
    if fill_day_stop and float(future.loc[fill_idx, "low"]) <= stop:
        outcome, exit_price, exit_date, hold = STOP, min(stop, fill_price), fill_date, 0
        lo = float(future.loc[fill_idx, "low"])
        after = after.iloc[0:0]
    for k, (_, b) in enumerate(after.iterrows(), start=1):
        if signal_exit_on is not None and b["date"] >= signal_exit_on:
            outcome, exit_price = SIGNAL_EXIT, float(b["open"])
        elif b["open"] <= stop:
            outcome, exit_price = STOP, float(b["open"])
        elif b["open"] >= target:
            outcome, exit_price = TARGET, float(b["open"])
        elif b["low"] <= stop:
            outcome, exit_price = STOP, stop
        elif b["high"] >= target:
            outcome, exit_price = TARGET, target
        elif k == horizon:
            outcome, exit_price = TIME, float(b["close"])
        if outcome != SIGNAL_EXIT:
            hi, lo = max(hi, float(b["high"])), min(lo, float(b["low"]))
        if outcome:
            exit_date, hold = b["date"], k
            break

    out["mfe_r"] = round((hi - fill_price) / risk, 2)
    out["mae_r"] = round((lo - fill_price) / risk, 2)
    if outcome is None:
        out["outcome"] = OPEN
        return out

    cost = 2 * COST_BPS_PER_SIDE / 1e4
    gross = exit_price / fill_price - 1
    out.update(outcome=outcome, exit_date=exit_date, exit_price=round(exit_price, 4),
               hold_sessions=hold,
               return_pct=round((gross - cost) * 100, 4),
               r_multiple=round((exit_price - fill_price - cost * fill_price) / risk, 3))
    return out


def simulate_ticker(plans: list[dict], bars: pd.DataFrame, sell_dates: list[date]) -> list[dict]:
    """
    Run a ticker's plans in report order with one trade at a time. A plan is
    SKIPPED_IN_TRADE while an earlier one is resting or held; the next entry may
    not fill on the session the previous trade exited.
    """
    results = []
    busy_until: Optional[date] = None   # last session the previous trade occupies
    blocked = False                     # previous trade still open/pending → skip the rest
    for plan in sorted(plans, key=lambda p: (p["report_date"], p["run_id"])):
        if blocked or (busy_until is not None and plan["report_date"] <= busy_until):
            results.append({**plan, "outcome": SKIPPED})
            continue
        earliest = None
        if busy_until is not None:
            nxt = bars[bars["date"] > busy_until]["date"]
            earliest = nxt.iloc[0] if not nxt.empty else None
            if earliest is None:
                results.append({**plan, "outcome": PENDING})
                continue
        res = simulate_trade(plan, bars, earliest, sell_dates)
        results.append({**plan, **res})
        if res["outcome"] in (OPEN, PENDING):
            blocked = True
        elif res["exit_date"] is not None:
            busy_until = res["exit_date"]
        elif res["outcome"] == NO_FILL:
            # The resting order occupied the entry window; a new plan inside it
            # would have replaced it, which the execution layer does not do.
            window = bars[bars["date"] >= plan["report_date"]]["date"].head(ENTRY_VALID_SESSIONS)
            busy_until = window.iloc[-1] if not window.empty else busy_until
    return results


# ── Statistics ─────────────────────────────────────────────────────────────

def summarize(trades: list[dict]) -> dict:
    """Aggregate statistics over closed trades, plus outcome counts for all plans."""
    closed = [t for t in trades if t.get("outcome") in CLOSED_OUTCOMES]
    attempted = [t for t in trades if t.get("outcome") not in (SKIPPED, PENDING)]
    filled = [t for t in attempted if t.get("fill_date") is not None]

    def stats(rows: list[dict]) -> Optional[dict]:
        if not rows:
            return None
        rs = [float(r["r_multiple"]) for r in rows]
        wins = [r for r in rs if r > 0]
        losses = [-r for r in rs if r <= 0]
        return {
            "n":              len(rs),
            "win_rate":       round(len(wins) / len(rs), 3),
            "avg_r":          round(sum(rs) / len(rs), 3),          # expectancy per trade
            "avg_win_r":      round(sum(wins) / len(wins), 3) if wins else None,
            "avg_loss_r":     round(-sum(losses) / len(losses), 3) if losses else None,
            "profit_factor":  round(sum(wins) / sum(losses), 2) if losses and sum(losses) > 0 else None,
            "avg_return_pct": round(sum(float(r["return_pct"]) for r in rows) / len(rows), 3),
            "avg_hold":       round(sum(r["hold_sessions"] for r in rows) / len(rows), 2),
        }

    def grouped(key: str) -> dict:
        groups: dict = {}
        for t in closed:
            groups.setdefault(t.get(key) or "UNKNOWN", []).append(t)
        return {k: stats(v) for k, v in sorted(groups.items())}

    outcomes: dict = {}
    for t in trades:
        outcomes[t.get("outcome")] = outcomes.get(t.get("outcome"), 0) + 1

    return {
        "plans":       len(trades),
        "fill_rate":   round(len(filled) / len(attempted), 3) if attempted else None,
        "outcomes":    outcomes,
        "overall":     stats(closed),
        "by_setup":    grouped("setup_type"),
        "by_strength": grouped("signal_strength"),
        "by_ticker":   grouped("ticker"),
    }


# ── Supabase I/O ───────────────────────────────────────────────────────────

PLAN_COLUMNS = ("run_id,ticker,report_date,signal,signal_strength,setup_type,"
                "entry_price,stop_price,target_price,horizon_days")


def _fetch_swing_reports(client: Client) -> list[dict]:
    """Every swing-mode report (BUY plans and SELL exits), latest run per ticker per day."""
    rows, start, page = [], 0, 1000
    while True:
        resp = (
            client.table("agent_reports")
            .select(PLAN_COLUMNS + ",created_at")
            .eq("trading_mode", "swing")
            .in_("signal", ["BUY", "SELL"])
            .order("created_at", desc=True)
            .range(start, start + page - 1)
            .execute()
        )
        rows.extend(resp.data or [])
        if len(resp.data or []) < page:
            break
        start += page
    latest: dict = {}
    for r in rows:   # created_at desc → first seen is the latest run that day
        latest.setdefault((r["ticker"], r["report_date"]), r)
    out = list(latest.values())
    for r in out:
        r["report_date"] = date.fromisoformat(str(r["report_date"])[:10])
    return out


def _fetch_bars(client: Client, tickers: list[str], since: date) -> pd.DataFrame:
    rows, start, page = [], 0, 1000
    while True:
        resp = (
            client.table("stock_prices")
            .select("ticker,date,open,high,low,close")
            .in_("ticker", tickers)
            .gte("date", since.isoformat())
            .order("date", desc=False)
            .range(start, start + page - 1)
            .execute()
        )
        rows.extend(resp.data or [])
        if len(resp.data or []) < page:
            break
        start += page
    df = pd.DataFrame(rows, columns=["ticker", "date", "open", "high", "low", "close"])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    return df


def run_swing_backtest(client: Client, backtest_run_id: str, dry_run: bool = False) -> dict:
    """
    Simulate every swing BUY plan on record and upsert one row per plan into
    swing_backtest_trades (re-simulated in full each run, so OPEN/PENDING rows
    settle as bars arrive). Returns {"trades": [...], "summary": {...}}.
    """
    reports = _fetch_swing_reports(client)
    plans = [r for r in reports if r["signal"] == "BUY"
             and all(r.get(k) is not None for k in ("entry_price", "stop_price", "target_price", "horizon_days"))]
    if not plans:
        logger.info("  No swing BUY plans on record yet — nothing to simulate.")
        return {"trades": [], "summary": summarize([])}

    tickers = sorted({p["ticker"] for p in plans})
    bars = _fetch_bars(client, tickers, min(p["report_date"] for p in plans))
    sells: dict = {}
    for r in reports:
        if r["signal"] == "SELL":
            sells.setdefault(r["ticker"], []).append(r["report_date"])

    trades = []
    for t in tickers:
        tb = bars[bars["ticker"] == t].sort_values("date").reset_index(drop=True)
        trades.extend(simulate_ticker([p for p in plans if p["ticker"] == t], tb, sells.get(t, [])))

    summary = summarize(trades)
    logger.info(f"  Swing plans: {len(plans)} across {len(tickers)} tickers — outcomes {summary['outcomes']}")

    if not dry_run:
        now = datetime.now(timezone.utc).isoformat()
        rows = [{
            "report_run_id":   t["run_id"],
            "ticker":          t["ticker"],
            "report_date":     t["report_date"].isoformat(),
            "signal_strength": t.get("signal_strength"),
            "setup_type":      t.get("setup_type"),
            "entry_price":     t["entry_price"],
            "stop_price":      t["stop_price"],
            "target_price":    t["target_price"],
            "horizon_days":    t["horizon_days"],
            "outcome":         t["outcome"],
            "fill_date":       t["fill_date"].isoformat() if t.get("fill_date") else None,
            "fill_price":      t.get("fill_price"),
            "exit_date":       t["exit_date"].isoformat() if t.get("exit_date") else None,
            "exit_price":      t.get("exit_price"),
            "hold_sessions":   t.get("hold_sessions"),
            "return_pct":      t.get("return_pct"),
            "r_multiple":      t.get("r_multiple"),
            "mfe_r":           t.get("mfe_r"),
            "mae_r":           t.get("mae_r"),
            "backtest_run_id": backtest_run_id,
            "updated_at":      now,
        } for t in trades]
        client.table("swing_backtest_trades").upsert(rows, on_conflict="report_run_id,ticker").execute()
        logger.info(f"  Saved {len(rows)} swing_backtest_trades rows")

    return {"trades": trades, "summary": summary}
