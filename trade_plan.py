"""
trade_plan.py — turns the day's swing reports into sized orders for the
account. Pure functions; run_trade_plan.py does the I/O.

Exits come first (they free capacity), for each position we opened (the
ledger knows its plan; a broker position with no ledger record is UNMANAGED and
left alone):
  EXIT_SIGNAL   today's swing report on the ticker is SELL
  EXIT_STOP     last close at/below the plan stop (the resting stop did not fire)
  EXIT_TARGET   last close at/above the plan target
  EXIT_TIME     horizon_days sessions completed since the fill
  PROTECT_STOP  otherwise, a resting GTC stop at the plan stop if none is working,
                from the fill day on (within the day-trade count, trade_guard)
Exits are MARKET orders at the open; the stop is a STOP order. With the live executor on
(executor_live) it makes EXECUTOR_EXITS itself at each pass, so the plan leaves them out.

Entries, for today's BUY plans plus yesterday's still-valid unfilled ones:
  - HOLD and SELL get no new money; risk_score >= 8 is excluded (risk manager)
  - effective risk = max(risk manager level, regime floor): CRITICAL → no new
    entries, HIGH → half risk per trade
  - qty = floor(equity × RISK_PER_TRADE_PCT × strength × level / (entry − stop)),
    capped by MAX_POSITION_PCT and the risk manager's max_position_pct
  - ranked by strength, then reward:risk; each is run through trade_guard in
    order, so caps count the entries approved before it.
"""

import math
from datetime import date
from typing import Optional

from price_checks import close_issue, entry_reasons, live_note
from swing_policy import sessions_missing
from trade_guard import (
    BUY, ENTRY, MAX_POSITION_PCT, RISK_PER_TRADE_PCT, SELL, GuardContext, check_order,
)

STRENGTH_RISK = {"STRONG": 1.0, "MODERATE": 0.75, "WEAK": 0.5}
LEVEL_RISK = {"LOW": 1.0, "MODERATE": 1.0, "HIGH": 0.5, "CRITICAL": 0.0}
RISK_ORDER = ["LOW", "MODERATE", "HIGH", "CRITICAL"]
EXCLUDE_RISK_SCORE = 8
EXECUTOR_EXITS = ("EXIT_STOP", "EXIT_TIME", "PROTECT_STOP")   # executor.py creates these rows itself

PROPOSED, BLOCKED = "PROPOSED", "BLOCKED"


def effective_level(risk_level: Optional[str], regime_floor: Optional[str]) -> str:
    """max(), never min(): the regime can only escalate the risk manager's level."""
    levels = [lv for lv in (risk_level, regime_floor) if lv in RISK_ORDER]
    return max(levels, key=RISK_ORDER.index) if levels else "MODERATE"


def size_entry(plan: dict, equity: float, level: str, risk_cap_pct: Optional[float]) -> tuple[int, float]:
    """(shares, dollars at risk to the stop). 0 shares when the trade cannot be sized."""
    per_share = float(plan["entry_price"]) - float(plan["stop_price"])
    if per_share <= 0 or equity <= 0:
        return 0, 0.0
    budget = equity * RISK_PER_TRADE_PCT / 100 \
        * STRENGTH_RISK.get(plan.get("signal_strength"), 0.5) * LEVEL_RISK.get(level, 1.0)
    qty = math.floor(budget / per_share)
    cap_pct = min(MAX_POSITION_PCT, risk_cap_pct) if risk_cap_pct is not None else MAX_POSITION_PCT
    qty = min(qty, math.floor(equity * cap_pct / 100 / float(plan["entry_price"])))
    qty = max(qty, 0)
    return qty, round(qty * per_share, 2)


def build_exits(ctx: GuardContext, todays_signals: dict, last_close: dict) -> tuple[list[dict], list[str]]:
    """Exit and protective-stop orders for managed positions; plus UNMANAGED tickers."""
    orders, unmanaged = [], []
    for t, pos in sorted(ctx.positions.items()):
        info = ctx.managed.get(t)
        qty = int(float(pos.get("qty") or 0))
        if not info:
            unmanaged.append(t)
            continue
        if qty <= 0:
            continue
        close = last_close.get(t)
        held = sessions_missing(info["opened_date"].isoformat(), ctx.today) or 0
        if todays_signals.get(t) == "SELL":
            intent = "EXIT_SIGNAL"
        elif close is not None and close <= info["stop_price"]:
            intent = "EXIT_STOP"
        elif close is not None and info.get("target_price") and close >= info["target_price"]:
            intent = "EXIT_TARGET"
        elif info.get("horizon_days") and held >= info["horizon_days"]:
            intent = "EXIT_TIME"
        elif not any(o["ticker"] == t and o["side"] == SELL for o in ctx.open_orders):
            intent = "PROTECT_STOP"
        else:
            continue
        protect = intent == "PROTECT_STOP"
        working_sells = [o for o in ctx.open_orders if o["ticker"] == t and o["side"] == SELL]
        orders.append({
            # A resting stop holds the shares; it must be cancelled before a market exit.
            "cancel_first": [o.get("id") for o in working_sells] if not protect else [],
            "ticker": t, "side": SELL, "intent": intent, "qty": qty,
            "order_type": "STOP" if protect else "MARKET",
            "time_in_force": "GTC" if protect else "DAY",
            "limit_price": None, "stop_price": info["stop_price"],
            "target_price": info.get("target_price"), "horizon_days": info.get("horizon_days"),
            "report_run_id": info.get("report_run_id"), "report_date": None,
            "risk_dollars": None,
            "note": f"held {held} session(s), last close {close}",
        })
    return orders, unmanaged


def build_entries(ctx: GuardContext, plans: list[dict], risk_by_ticker: dict, level: str,
                  quotes: Optional[dict] = None, stored_closes: Optional[dict] = None) -> list[dict]:
    """
    Sized, guard-checked entry orders, best first. With `quotes` (price_checks.
    parse_quotes of Robinhood quotes) each entry is also verified against
    Robinhood: stored_closes {ticker: (close, date)} must match the official
    close, and the live price is passed to the guard and noted on the order.
    """
    rank = {"STRONG": 0, "MODERATE": 1, "WEAK": 2}
    plans = sorted(plans, key=lambda p: (rank.get(p.get("signal_strength"), 3), -(p.get("risk_reward") or 0)))
    orders = []
    for p in plans:
        risk = risk_by_ticker.get(p["ticker"]) or {}
        order = {
            "ticker": p["ticker"], "side": BUY, "intent": ENTRY,
            "order_type": "LIMIT", "time_in_force": "DAY",
            "limit_price": float(p["entry_price"]), "stop_price": float(p["stop_price"]),
            "target_price": float(p["target_price"]), "horizon_days": p["horizon_days"],
            "report_run_id": p["run_id"], "report_date": p["report_date"],
            "signal_strength": p.get("signal_strength"), "setup_type": p.get("setup_type"),
        }
        reasons = []
        if (risk.get("risk_score") or 0) >= EXCLUDE_RISK_SCORE:
            reasons.append("RISK_SCORE_EXCLUDED")
        if LEVEL_RISK.get(level, 1.0) == 0:
            reasons.append("RISK_LEVEL_CRITICAL")
        qty, risk_dollars = size_entry(p, ctx.equity, level, risk.get("max_position_pct"))
        order.update(qty=qty, risk_dollars=risk_dollars,
                     note=f"{p.get('signal_strength')} {p.get('setup_type')}, R:R {p.get('risk_reward')}, level {level}")
        q = (quotes or {}).get(p["ticker"])
        if quotes is not None:
            stored = (stored_closes or {}).get(p["ticker"], (None, None))
            problem = close_issue(stored[0], stored[1], q)
            reasons += entry_reasons(q, problem)
            extra = [x for x in (live_note(order, q), problem and f"price check: {problem}") if x]
            if extra:
                order["note"] += " · " + " · ".join(extra)
        if qty < 1 and not reasons:
            reasons.append("TOO_SMALL")
        if not reasons:
            reasons = check_order(order, ctx, quote=q["live"] if q else None)
        order["guard_reasons"] = reasons
        order["status"] = BLOCKED if reasons else PROPOSED
        if not reasons:
            ctx.batch_entries.append(order)
        orders.append(order)
    return orders


def build_plan(ctx: GuardContext, plans: list[dict], todays_signals: dict, last_close: dict,
               risk_by_ticker: dict, level: str, quotes: Optional[dict] = None,
               stored_closes: Optional[dict] = None, executor_live: bool = False) -> dict:
    exits, unmanaged = build_exits(ctx, todays_signals, last_close)
    if executor_live:
        exits = [o for o in exits if o["intent"] not in EXECUTOR_EXITS]
    for o in exits:
        o["guard_reasons"] = check_order(o, ctx)
        o["status"] = BLOCKED if o["guard_reasons"] else PROPOSED
    for o in exits:
        q = (quotes or {}).get(o["ticker"])
        if q and q.get("live"):
            o["note"] = f"{o['note']} · live {q['live']:.2f}"
            if o["intent"] == "PROTECT_STOP" and q["live"] <= o["stop_price"]:
                o["note"] += " · at or below the stop: entered now, it triggers at the open"
    entries = build_entries(ctx, plans, risk_by_ticker, level, quotes, stored_closes)
    return {"exits": exits, "entries": entries, "unmanaged": unmanaged, "level": level}
