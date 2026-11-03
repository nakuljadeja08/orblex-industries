"""
trade_guard.py — the pre-trade safety checks every swing order must pass, both
when the plan is built and again immediately before it is placed.

Pure functions, no I/O: the caller assembles a GuardContext from the broker
snapshot (positions, today's fills and open orders), our own fill ledger
(trade_fills), and the kill-switch row (trading_controls). Both the broker's
fills and the ledger are consulted for the same-day rules, so a fill missing
from one source still blocks.

Selling on the fill day is allowed (a protective stop can rest from the fill), buying
back after a sale is not:
  SAME_DAY_REBUY   no buy of a ticker sold today
  PDT_LIMIT        no sell of a ticker bought today once it could be the 4th day trade in
                   5 sessions (equity under $25k), or when that count is unknown
  SAME_DAY_SELL    only while exec_limits.ALLOW_SAME_DAY_EXIT is False: no sell (market,
                   limit or a resting stop) of a ticker bought today
  OPPOSITE_ORDER   no sell while a buy order for the ticker is working today (it could
                   fill first), and no buy while a sell order is working

Plus account limits, a kill switch, and order sanity. check_order() returns a
list of reason codes; an empty list means the order may go.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from swing_policy import ENTRY_VALID_SESSIONS, sessions_missing

# ── Limits: one source of truth in exec_limits.py (EXECUTOR_DESIGN.md §1.3) ───
from exec_limits import (  # noqa: E402  (re-exported: trade_plan / universe_selector import them from here)
    ALLOW_SAME_DAY_EXIT, DAILY_LOSS_HALT_PCT, DRAWDOWN_HALT_PCT, MAX_NEW_ENTRIES_DAY, MAX_OPEN_POSITIONS,
    MAX_OPEN_RISK_PCT, MAX_POSITION_PCT, PDT_EQUITY, PDT_MAX_DAY_TRADES, RISK_PER_TRADE_PCT,
)
UNMANAGED_RISK_PCT   = 10.0   # assumed risk on a position with no stop on record

BUY, SELL = "BUY", "SELL"
ENTRY = "ENTRY"
EXIT_INTENTS = ("EXIT_SIGNAL", "EXIT_TIME", "EXIT_STOP", "EXIT_TARGET", "PROTECT_STOP")

# Reason codes. HALT_REASONS block entries only — exits are always allowed to
# reduce risk, subject to the day-trade count.
HALTED, DAILY_LOSS, DRAWDOWN = "HALTED", "DAILY_LOSS_HALT", "DRAWDOWN_HALT"
HALT_REASONS = (HALTED, DAILY_LOSS, DRAWDOWN)


@dataclass
class GuardContext:
    today: date
    equity: float
    buying_power: float
    positions: dict                    # ticker -> {"qty", "avg_cost", "last_price"?}
    bought_today: set                  # tickers with a BUY fill today (broker ∪ ledger)
    sold_today: set                    # tickers with a SELL fill today (broker ∪ ledger)
    open_orders: list                  # broker working orders: {"ticker","side","type","qty"}
    managed: dict = field(default_factory=dict)   # ticker -> {"stop_price", "opened_date"}
    universe: Optional[set] = None
    halted: bool = False
    halt_reason: Optional[str] = None
    day_pnl: Optional[float] = None
    equity_peak: Optional[float] = None
    # Entries already approved earlier in the same batch (counted against caps).
    batch_entries: list = field(default_factory=list)
    # Same-day round trips over today + the 4 prior sessions; None = unknown (fails closed).
    day_trades_5_sessions: Optional[int] = None


def position_risk(ticker: str, pos: dict, ctx: GuardContext) -> float:
    """Dollar risk to the stop on an open position; a conservative guess if unmanaged."""
    qty = float(pos.get("qty") or 0)
    price = float(pos.get("last_price") or pos.get("avg_cost") or 0)
    stop = (ctx.managed.get(ticker) or {}).get("stop_price")
    if stop is None:
        return qty * price * UNMANAGED_RISK_PCT / 100
    return max(0.0, qty * (price - float(stop)))


def halt_reasons(ctx: GuardContext) -> list[str]:
    """Account-level reasons that stop all new entries."""
    out = []
    if ctx.halted:
        out.append(HALTED)
    if ctx.day_pnl is not None and ctx.equity > 0 and -ctx.day_pnl / ctx.equity * 100 >= DAILY_LOSS_HALT_PCT:
        out.append(DAILY_LOSS)
    if ctx.equity_peak and ctx.equity_peak > 0 and (1 - ctx.equity / ctx.equity_peak) * 100 >= DRAWDOWN_HALT_PCT:
        out.append(DRAWDOWN)
    return out


def _working(ctx: GuardContext, ticker: str, side: str) -> bool:
    return any(o["ticker"] == ticker and o["side"] == side for o in ctx.open_orders)


def _day_trade_reasons(t: str, ctx: GuardContext) -> list[str]:
    """Reasons a sell of a ticker bought today may not go. Sells already working on other
    tickers bought today count too: each could still fire today and become a day trade."""
    if not ALLOW_SAME_DAY_EXIT:
        return ["SAME_DAY_SELL"]
    if ctx.equity >= PDT_EQUITY:
        return []
    if ctx.day_trades_5_sessions is None:
        return ["PDT_LIMIT"]
    resting = {o["ticker"] for o in ctx.open_orders if o["side"] == SELL and o["ticker"] != t} & ctx.bought_today
    return ["PDT_LIMIT"] if ctx.day_trades_5_sessions + len(resting) + 1 > PDT_MAX_DAY_TRADES else []


def check_order(order: dict, ctx: GuardContext, quote: Optional[float] = None) -> list[str]:
    """
    Reasons this order must not be placed (empty list = allowed). `quote` is a
    live last/ask price, given at placement time; plan-time checks pass None.
    """
    t, side, intent = order["ticker"], order["side"], order["intent"]
    qty = order.get("qty") or 0
    reasons: list[str] = []

    if qty <= 0 or int(qty) != qty:
        reasons.append("BAD_QTY")
    if intent != ENTRY and intent not in EXIT_INTENTS:
        reasons.append("UNKNOWN_INTENT")

    # ── Sells: same-day exits count as day trades ────────────────────────
    if side == SELL:
        opened = (ctx.managed.get(t) or {}).get("opened_date")
        if t in ctx.bought_today or (opened is not None and opened >= ctx.today):
            reasons += _day_trade_reasons(t, ctx)
        if _working(ctx, t, BUY):
            reasons.append("OPPOSITE_ORDER")
        held = float((ctx.positions.get(t) or {}).get("qty") or 0)
        if qty > held:
            reasons.append("SELL_EXCEEDS_POSITION")
        return sorted(set(reasons))

    # ── Entries ──────────────────────────────────────────────────────────
    if t in ctx.sold_today:
        reasons.append("SAME_DAY_REBUY")
    if _working(ctx, t, SELL):
        reasons.append("OPPOSITE_ORDER")
    reasons += halt_reasons(ctx)

    if ctx.universe is not None and t not in ctx.universe:
        reasons.append("NOT_IN_UNIVERSE")
    if t in ctx.positions or _working(ctx, t, BUY) or any(b["ticker"] == t for b in ctx.batch_entries):
        reasons.append("ALREADY_IN_TICKER")

    # An entry plan is live for ENTRY_VALID_SESSIONS sessions from its report date.
    rd = order.get("report_date")
    if rd is None or rd > ctx.today or (rd < ctx.today and
                                         (sessions_missing(rd.isoformat(), ctx.today) or 0) >= ENTRY_VALID_SESSIONS - 1):
        reasons.append("STALE_PLAN")

    limit, stop = order.get("limit_price"), order.get("stop_price")
    if order.get("order_type") != "LIMIT" or not limit:
        reasons.append("ENTRY_NOT_LIMIT")
    if not stop or not limit or stop >= limit:
        reasons.append("BAD_LEVELS")
    if quote is not None and stop and quote <= stop:
        reasons.append("QUOTE_BELOW_STOP")

    # Caps — open positions + working buys + entries approved earlier in the batch.
    working_buys = {o["ticker"] for o in ctx.open_orders if o["side"] == BUY}
    n_positions = len(set(ctx.positions) | working_buys | {b["ticker"] for b in ctx.batch_entries})
    if n_positions + 1 > MAX_OPEN_POSITIONS:
        reasons.append("MAX_POSITIONS")
    n_entries = len(ctx.bought_today | working_buys | {b["ticker"] for b in ctx.batch_entries})
    if n_entries + 1 > MAX_NEW_ENTRIES_DAY:
        reasons.append("MAX_ENTRIES_TODAY")

    if limit and qty and ctx.equity > 0:
        notional = qty * limit
        if notional > ctx.equity * MAX_POSITION_PCT / 100 + 1e-6:
            reasons.append("POSITION_TOO_LARGE")
        committed = sum(b["qty"] * b["limit_price"] for b in ctx.batch_entries)
        if notional > ctx.buying_power - committed + 1e-6:
            reasons.append("BUYING_POWER")
        if stop and stop < limit:
            open_risk = sum(position_risk(tk, p, ctx) for tk, p in ctx.positions.items())
            open_risk += sum(b["qty"] * (b["limit_price"] - b["stop_price"]) for b in ctx.batch_entries)
            if open_risk + qty * (limit - stop) > ctx.equity * MAX_OPEN_RISK_PCT / 100 + 1e-6:
                reasons.append("OPEN_RISK_LIMIT")

    return sorted(set(reasons))
