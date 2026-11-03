"""
broker_snapshot.py — turns raw Robinhood MCP responses into the account
snapshot run_trade_plan.py reads, so the mapping is code, not hand-copying.

The execution session saves each MCP response as JSON (the "data" object or the
whole response) and runs:

  python broker_snapshot.py --portfolio portfolio.json --positions positions.json \\
      --orders orders.json [--quotes quotes.json ...] > snapshot.json

Rules that matter for the guard:
  - buying_power is the UNLEVERAGED figure. The agentic account is a (limited)
    margin account; sizing must never plan on borrowed money.
  - A position with intraday_quantity > 0 was bought today → counts as a BUY
    fill today, so the same-day-sell rule holds even if the order list is
    paginated or late.
  - Any order with filled quantity whose last activity is today (New York)
    counts as a fill today, in its direction. Partially filled orders count.
  - Open orders are those still working: new, queued, confirmed, unconfirmed,
    partially_filled.
When in doubt the mapping errs toward blocking (more "today" fills, more open
orders), never toward allowing.
"""

import argparse
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from price_checks import parse_quotes

NY = ZoneInfo("America/New_York")
OPEN_STATES = {"new", "queued", "confirmed", "unconfirmed", "partially_filled"}
TYPE_MAP = {"limit": "LIMIT", "market": "MARKET", "stop_market": "STOP", "stop_limit": "STOP_LIMIT"}


def _data(obj):
    return obj.get("data", obj) if isinstance(obj, dict) else obj


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _ny_date(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(NY).date()
    except ValueError:
        return None


def _order_type(o: dict) -> str:
    t = (o.get("type") or "").lower()
    trig = (o.get("trigger") or "").lower()
    # Robinhood encodes a stop as type=market|limit with trigger=stop.
    if trig == "stop":
        return "STOP_LIMIT" if t in ("limit", "stop_limit") else "STOP"
    return TYPE_MAP.get(t) or t.upper() or "UNKNOWN"


def build_snapshot(portfolio: dict, positions: dict, orders: dict, quotes: dict | None = None,
                   now: datetime | None = None, source: str = "robinhood") -> dict:
    now = now or datetime.now(NY)
    today = now.astimezone(NY).date()
    pf = _data(portfolio)
    bp = pf.get("buying_power") or {}
    # Unleveraged only: never plan on margin. Fall back to cash, then to 0.
    buying_power = _f(bp.get("unleveraged_buying_power"), None)
    if buying_power is None:
        buying_power = _f(pf.get("cash"), 0.0)

    # quotes: one get_equity_quotes response or a list of them (≤20 symbols each).
    qlist = quotes if isinstance(quotes, list) else [quotes] if quotes else []
    last = {sym: q["live"] for sym, q in parse_quotes(*qlist).items() if q.get("live")}

    snap_positions, fills = [], []
    for p in _data(positions).get("positions", []):
        sym = (p.get("symbol") or "").upper()
        qty = _f(p.get("quantity"))
        if not sym or qty <= 0:
            continue
        row = {"ticker": sym, "qty": qty, "avg_cost": _f(p.get("average_buy_price"), None),
               "sellable": _f(p.get("shares_available_for_sells"), qty)}
        if sym in last:
            row["last_price"] = last[sym]
        snap_positions.append(row)
        if _f(p.get("intraday_quantity")) > 0:
            fills.append({"ticker": sym, "side": "BUY", "qty": _f(p.get("intraday_quantity")),
                          "source": "intraday_quantity"})

    open_orders = []
    for o in _data(orders).get("orders", []):
        sym = (o.get("symbol") or "").upper()
        side = (o.get("side") or "").upper()
        state = (o.get("state") or "").lower()
        if not sym or side not in ("BUY", "SELL"):
            continue
        filled = _f(o.get("cumulative_quantity"))
        if filled > 0 and _ny_date(o.get("last_transaction_at") or o.get("updated_at")) == today:
            fills.append({"ticker": sym, "side": side, "qty": filled,
                          "price": _f(o.get("average_price"), None), "order_id": o.get("id")})
        if state in OPEN_STATES:
            open_orders.append({"id": o.get("id"), "ticker": sym, "side": side, "type": _order_type(o),
                                "qty": _f(o.get("quantity")) - filled,
                                "limit_price": _f(o.get("price"), None),
                                "stop_price": _f(o.get("stop_price"), None)})

    return {
        "as_of": now.astimezone(NY).isoformat(),
        "source": source,
        "equity": _f(pf.get("total_value")),
        "buying_power": round(buying_power, 2),
        "cash": _f(pf.get("cash")),
        "pending_deposits": _f(pf.get("pending_deposits")),
        "positions": snap_positions,
        "fills_today": fills,
        "open_orders": open_orders,
    }


def main():
    ap = argparse.ArgumentParser(description="Robinhood MCP responses → run_trade_plan snapshot")
    ap.add_argument("--portfolio", required=True)
    ap.add_argument("--positions", required=True)
    ap.add_argument("--orders", required=True)
    ap.add_argument("--quotes", nargs="+", help="Saved get_equity_quotes response(s)")
    ap.add_argument("--out", help="Write here instead of stdout")
    a = ap.parse_args()

    def load(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    snap = build_snapshot(load(a.portfolio), load(a.positions), load(a.orders),
                          [load(q) for q in a.quotes] if a.quotes else None)
    text = json.dumps(snap, indent=2)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text + "\n")


if __name__ == "__main__":
    main()
