"""
exec_state.py — reads what the rate ceilings and the mandate need from Supabase
(EXECUTOR_DESIGN.md §1.3). The pure rules live in exec_limits.py.

  load_mandate(sb, today)      today's trading_mandates row → Mandate, or None
  load_exec_state(sb, today)   ExecState over today + the 4 prior NYSE sessions, from
                               exec_events (what the executor submitted) and trade_fills
                               joined to trade_orders (what actually traded, including
                               orders placed by hand). Any read failure → complete=False,
                               and exec_limits.check_submission then refuses everything.
"""

import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from exec_limits import ENTRY, EXIT_INTENTS, ExecState, Mandate

logger = logging.getLogger(__name__)
WINDOW_SESSIONS = 5


def last_sessions(today: date, n: int = WINDOW_SESSIONS) -> list[date]:
    """today and the n−1 NYSE sessions before it (weekdays if the calendar is unavailable)."""
    try:
        import pandas_market_calendars as mcal
        sched = mcal.get_calendar("NYSE").schedule(start_date=today - timedelta(days=14), end_date=today)
        days = [d.date() for d in sched.index if d.date() <= today]
    except Exception:
        days = [today - timedelta(days=i) for i in range(14, -1, -1)
                if (today - timedelta(days=i)).weekday() < 5]
    if today not in days:
        days.append(today)
    return sorted(days)[-n:]


def load_mandate(sb, today: date) -> Optional[Mandate]:
    r = sb.table("trading_mandates").select("*").eq("mandate_date", today.isoformat()).limit(1).execute().data
    return Mandate.from_row(r[0]) if r else None


def build_exec_state(today: date, sessions: list[date], events: list[dict], fills: list[dict],
                     orders: dict) -> ExecState:
    """Pure: events/fills as stored rows, orders = {id: trade_orders row}."""
    st = ExecState(today=today)
    win = set(sessions)

    entry_orders_by_day: dict = defaultdict(set)      # day → {order_id or ("fill", ticker)}
    entry_tickers_today: set = set()
    risk_ids_today: set = set()

    for e in events:
        d = date.fromisoformat(str(e["trade_date"])[:10])
        oid, kind = e.get("order_id"), e["kind"]
        if kind in ("SUBMIT", "RESUBMIT"):
            st.submits_by_order[oid] = st.submits_by_order.get(oid, 0) + 1
        if d == today and kind in ("SUBMIT", "RESUBMIT", "REPLACE"):
            st.orders_submitted_today += 1
        if d == today and kind in ("CANCEL", "REPLACE"):
            st.cancel_replace_today[oid] = st.cancel_replace_today.get(oid, 0) + 1
        # Only an ACCEPTED submit is an entry. A failed attempt counts toward the order rate
        # and the retry limit above, but must not use up an entry slot or the risk budget,
        # or its own retry would be refused by it.
        if e.get("intent") == ENTRY and kind in ("SUBMIT", "RESUBMIT") and e.get("ok", True) and d in win:
            entry_orders_by_day[d].add(oid)
            if d == today:
                entry_tickers_today.add(e["ticker"])
                risk_ids_today.add(oid)

    by_day_side: dict = defaultdict(lambda: defaultdict(set))
    exits = []
    for f in fills:
        d = date.fromisoformat(str(f["trade_date"])[:10])
        if d not in win:
            continue
        o = orders.get(f.get("order_id")) or {}
        intent = o.get("intent")
        by_day_side[d][f["side"]].add(f["ticker"])
        if f["side"] == "BUY" and intent in (ENTRY, None):        # an untracked buy counts as an entry
            entry_orders_by_day[d].add(f.get("order_id") or ("fill", f["ticker"]))
            if d == today:
                entry_tickers_today.add(f["ticker"])
                if f.get("order_id"):
                    risk_ids_today.add(f["order_id"])
        if f["side"] == "SELL" and d == today:
            exits.append((str(f.get("filled_at") or ""), intent if intent in EXIT_INTENTS else "UNKNOWN_EXIT"))

    st.entries_today = entry_tickers_today
    st.entries_5_sessions = sum(len(v) for v in entry_orders_by_day.values())
    st.entry_risk_today = round(sum(float((orders.get(i) or {}).get("risk_dollars") or 0) for i in risk_ids_today), 2)
    st.exits_today = [intent for _, intent in sorted(exits)]
    st.day_trades_5_sessions = sum(len(s["BUY"] & s["SELL"]) for s in by_day_side.values())
    return st


def load_exec_state(sb, today: date) -> ExecState:
    sessions = last_sessions(today)
    since = sessions[0].isoformat()
    try:
        events = sb.table("exec_events").select("*").gte("trade_date", since).execute().data or []
        fills = (sb.table("trade_fills").select("order_id,ticker,side,trade_date,filled_at")
                 .gte("trade_date", since).execute().data or [])
        ids = sorted({x["order_id"] for x in events + fills if x.get("order_id")})
        orders = {}
        if ids:
            for o in sb.table("trade_orders").select("id,intent,risk_dollars,ticker").in_("id", ids).execute().data or []:
                orders[o["id"]] = o
    except Exception as e:
        logger.error(f"  execution state unavailable ({e}); failing closed")
        return ExecState(today=today, complete=False)
    return build_exec_state(today, sessions, events, fills, orders)
