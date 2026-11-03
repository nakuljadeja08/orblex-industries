"""
robinhood_broker.py — the live Robinhood account through the executor's broker interface,
READ-ONLY (EXECUTOR_DESIGN.md phase 4), plus DryRunBroker.

  RobinhoodBroker  a snapshot of the agentic account fetched through rh_client (read-only tools
                   only): positions priced with live quotes, and recent orders. An order
                   the manual flow already linked to a plan order (trade_orders.broker_order_id,
                   written by sync_fills) shows up as "wi-<id>", exactly as if the executor
                   had placed it; anything else is "rh-<broker id>" (untracked).
                   submit() and cancel() REFUSE: this class cannot trade.
  DryRunBroker     wraps it. submit()/cancel() only RECORD what would have been sent and
                   show it in orders() as if it were working, so a second loop pass behaves
                   exactly as it would live. Nothing reaches Robinhood.
  LiveRobinhoodBroker  phase 5: the same view plus OCO groups and pending approvals, and a
                   submit()/cancel() that really send, through rh_trader (switch + approval
                   queue checks). Every order carries ref_id = uuid5("wi-<id>"), so Robinhood
                   deduplicates a retry and the next run recognises its own orders.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from accounts import AGENTIC_ACCOUNT
from broker import AccountView, BrokerError, BrokerOrder, OrderTicket, ref_id
from price_checks import parse_quotes

NY = ZoneInfo("America/New_York")
LIVE = {"queued", "confirmed", "unconfirmed", "partially_filled", "new", "pending"}
DEAD = {"cancelled", "rejected", "failed", "voided", "expired"}


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _data(x):
    return x.get("data", x) if isinstance(x, dict) else {}


def _ny_date(ts) -> Optional[date]:
    if not ts:
        return None
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(NY).date()


def _kind(o: dict) -> str:
    t, trig = (o.get("type") or "").lower(), (o.get("trigger") or "").lower()
    if trig == "stop" or t in ("stop_market", "stop_limit"):
        return "STOP_LIMIT" if t in ("limit", "stop_limit") else "STOP"
    return "LIMIT" if t == "limit" else "MARKET"


class RobinhoodBroker:
    """Read-only view of the agentic account. Build it from saved/fetched MCP responses."""

    def __init__(self, portfolio: dict, positions: dict, orders: dict, quotes: list,
                 links: dict, since: date):
        """links: Robinhood order id → trade_orders.id; since: oldest order date to show
        (older orders are already reflected in positions)."""
        self._quotes = parse_quotes(*quotes) if quotes else {}
        pf = _data(portfolio)
        self.cash = _f(pf.get("cash"), 0.0)
        self.total_value = _f(pf.get("total_value"))
        self._positions = {}
        for p in _data(positions).get("positions", []) or []:
            qty = _f(p.get("quantity"), 0.0)
            if qty and qty > 0:
                t = p["symbol"].upper()
                q = self._quotes.get(t) or {}
                last = q.get("live") or q.get("official_close") or _f(p.get("average_buy_price"))
                self._positions[t] = {"qty": int(qty) if float(qty).is_integer() else qty,
                                      "avg_cost": _f(p.get("average_buy_price")), "last_price": last}
        self._orders = []
        for o in (_data(orders).get("orders") or _data(orders).get("results") or []):
            state = (o.get("state") or "").lower()
            placed = _ny_date(o.get("created_at"))
            if state not in LIVE and (placed is None or placed < since):
                continue
            oid = o.get("id")
            cid = f"wi-{links[oid]}" if oid in links else f"rh-{oid}"
            cum = _f(o.get("cumulative_quantity"), 0.0) or 0.0
            qty = _f(o.get("quantity"), 0.0) or 0.0
            if state == "filled":
                st = "FILLED"
            elif state in LIVE:
                st = "WORKING"
            else:
                st = "CANCELLED" if state in DEAD and cum == 0 else ("FILLED" if cum >= qty and qty else "CANCELLED")
            ticket = OrderTicket(cid, (o.get("symbol") or "").upper(), (o.get("side") or "").upper(), _kind(o),
                                 int(qty) if float(qty).is_integer() else qty,
                                 "GTC" if (o.get("time_in_force") or "").lower() == "gtc" else "DAY",
                                 limit_price=_f(o.get("price")), stop_price=_f(o.get("stop_price")))
            self._orders.append(BrokerOrder(broker_order_id=oid, ticket=ticket, state=st,
                                            filled_qty=int(cum) if float(cum).is_integer() else cum,
                                            fill_price=_f(o.get("average_price")),
                                            fill_date=_ny_date(o.get("last_transaction_at")) if cum else None,
                                            placed_on=placed))

    # ── Broker interface ────────────────────────────────────────────────────
    def account(self) -> AccountView:
        equity = self.total_value if self.total_value is not None else \
            self.cash + sum(p["qty"] * (p["last_price"] or 0) for p in self._positions.values())
        return AccountView(equity=round(equity, 2), cash=round(self.cash, 2), positions=dict(self._positions))

    def orders(self) -> list:
        return list(self._orders)

    def find(self, client_order_id: str) -> Optional[BrokerOrder]:
        return next((o for o in self._orders if o.ticket.client_order_id == client_order_id), None)

    def submit(self, ticket: OrderTicket) -> BrokerOrder:
        raise BrokerError("RobinhoodBroker is read-only (phase 4 is a dry run)")

    def cancel(self, broker_order_id: str) -> BrokerOrder:
        raise BrokerError("RobinhoodBroker is read-only (phase 4 is a dry run)")


@dataclass
class DryRunBroker:
    inner: object
    would: list = field(default_factory=list)        # [{"action": "SUBMIT"|"CANCEL", ...}]
    _orders: dict = field(default_factory=dict)      # client id → BrokerOrder shown as WORKING
    _cancelled: set = field(default_factory=set)     # real broker ids we would have cancelled
    _seq: int = 0

    def account(self) -> AccountView:
        return self.inner.account()

    def orders(self) -> list:
        out = []
        for o in self.inner.orders():
            if o.broker_order_id in self._cancelled and o.state == "WORKING":
                o = BrokerOrder(o.broker_order_id, o.ticket, "CANCELLED", o.filled_qty, o.fill_price, o.fill_date,
                                o.fill_leg, o.placed_on)
            out.append(o)
        return out + list(self._orders.values())

    def find(self, client_order_id: str) -> Optional[BrokerOrder]:
        return self._orders.get(client_order_id) or self.inner.find(client_order_id)

    def submit(self, ticket: OrderTicket) -> BrokerOrder:
        if ticket.client_order_id in self._orders:
            return self._orders[ticket.client_order_id]
        self._seq += 1
        o = BrokerOrder(broker_order_id=f"dry-{self._seq}", ticket=ticket, state="WORKING")
        self._orders[ticket.client_order_id] = o
        self.would.append({"action": "SUBMIT", "client_order_id": ticket.client_order_id, "ticker": ticket.ticker,
                           "side": ticket.side, "type": ticket.order_type, "qty": ticket.qty,
                           "limit": ticket.limit_price, "stop": ticket.stop_price,
                           "take_profit": ticket.take_profit_price, "tif": ticket.time_in_force})
        return o

    def cancel(self, broker_order_id: str) -> BrokerOrder:
        for cid, o in self._orders.items():
            if o.broker_order_id == broker_order_id:
                o.state = "CANCELLED"
                return o
        real = next((o for o in self.inner.orders() if o.broker_order_id == broker_order_id), None)
        if real is None:
            raise BrokerError(f"unknown order {broker_order_id}")
        self._cancelled.add(broker_order_id)
        self.would.append({"action": "CANCEL", "broker_order_id": broker_order_id, "ticker": real.ticket.ticker,
                           "type": real.ticket.order_type})
        return real


# ── Phase 5: live ─────────────────────────────────────────────────────────────

def _px(x) -> Optional[str]:
    """A price as Robinhood wants it. Refuse (never round) a price that isn't on the tick."""
    if x is None:
        return None
    v = float(x)
    tick = round(v, 2) if v >= 1 else round(v, 4)
    if abs(tick - v) > 1e-6:
        raise BrokerError(f"price {v} is not on a valid tick")
    return f"{tick:.2f}" if v >= 1 else f"{tick:.4f}"


def _find_key(x, key):
    """First value of `key` anywhere in a nested response (shapes vary by tool)."""
    if isinstance(x, dict):
        if x.get(key):
            return x[key]
        for v in x.values():
            r = _find_key(v, key)
            if r:
                return r
    elif isinstance(x, list):
        for v in x:
            r = _find_key(v, key)
            if r:
                return r
    return None


class LiveRobinhoodBroker(RobinhoodBroker):
    """call: a SYNC function (tool name, args) → response, normally rh_trader.TradingRobinhood.call
    run on the session's event loop. cids: every "wi-<id>" in the ledger, to recognise our orders
    by ref_id. advanced / approvals: get_advanced_orders and get_trade_approvals responses."""

    def __init__(self, portfolio, positions, orders, quotes, links, since, *, advanced=None, approvals=None,
                 cids=(), call=None, account=AGENTIC_ACCOUNT):
        super().__init__(portfolio, positions, orders, quotes, links, since)
        self._call, self.account_number = call, account
        self.sent: list = []                         # every request sent and what came back
        self.kinds: dict = {}                        # broker id → "equity" | "advanced" | "approval"
        refs = {ref_id(c): c for c in cids}
        raw = _data(orders).get("orders") or _data(orders).get("results") or []
        by_ref = {o.get("id"): refs.get(str(o.get("ref_id") or "").lower()) for o in raw}

        legs, groups = set(), []
        for a in _data(advanced or {}).get("advanced_orders") or []:
            for link in a.get("advanced_order_links") or []:
                legs.add(link.get("order_id") or (link.get("order") or {}).get("id"))
            g = self._oco(a, refs, links, since)
            if g is not None:
                groups.append(g)
                self.kinds[g.broker_order_id] = "advanced"
        kept = []
        for o in self._orders:
            if o.broker_order_id in legs:
                continue                             # shown once, as its OCO group
            if by_ref.get(o.broker_order_id):
                o.ticket.client_order_id = by_ref[o.broker_order_id]
            self.kinds[o.broker_order_id] = "equity"
            kept.append(o)
        pending = []
        for ap in _data(approvals or {}).get("approvals") or []:
            aid = ap.get("approval_id") or ap.get("id")
            if not aid:
                continue
            bid = f"approval:{aid}"
            r = str(_find_key(ap, "ref_id") or "").lower()
            cid = refs.get(r) or (f"wi-{links[bid]}" if bid in links else f"rh-{bid}")
            qty = _f(_find_key(ap, "quantity"), 0.0) or 0.0
            t = OrderTicket(cid, str(_find_key(ap, "symbol") or "").upper(), str(_find_key(ap, "side") or "").upper(),
                            "OCO" if _find_key(ap, "take_profit_limit_price") else _kind(ap), qty)
            pending.append(BrokerOrder(broker_order_id=bid, ticket=t, state="WORKING"))
            self.kinds[bid] = "approval"
        self._orders = kept + groups + pending

    def _oco(self, a: dict, refs: dict, links: dict, since: date) -> Optional[BrokerOrder]:
        legs = [l.get("order") or {} for l in a.get("advanced_order_links") or []]
        stop = next((l for l in legs if _kind(l) in ("STOP", "STOP_LIMIT")), {})
        tp = next((l for l in legs if _kind(l) == "LIMIT"), {})
        anyleg = stop or tp
        placed = _ny_date(a.get("created_at") or anyleg.get("created_at"))
        filled = next((l for l in legs if (l.get("state") or "").lower() == "filled"), None)
        live = not filled and (any((l.get("state") or "").lower() in LIVE for l in legs)
                               or (a.get("state") or "").lower() in LIVE)
        when = _ny_date(filled.get("last_transaction_at")) if filled else placed
        if not live and (when is None or when < since):
            return None
        aid = a.get("id")
        cid = refs.get(str(a.get("ref_id") or "").lower()) or (f"wi-{links[aid]}" if aid in links else f"rh-{aid}")
        qty = _f(anyleg.get("quantity") or a.get("quantity"), 0.0) or 0.0
        qty = int(qty) if float(qty).is_integer() else qty
        t = OrderTicket(cid, (anyleg.get("symbol") or a.get("symbol") or "").upper(), "SELL", "OCO", qty,
                        "GTC" if (anyleg.get("time_in_force") or "").lower() == "gtc" else "DAY",
                        stop_price=_f(stop.get("stop_price")), take_profit_price=_f(tp.get("price")))
        if filled:
            # Carry the filled LEG's id: that is the equity order sync_fills records the fill
            # under, so the executor's own sync never counts the same fill twice.
            cum = _f(filled.get("cumulative_quantity"), 0.0) or 0.0
            return BrokerOrder(filled.get("id"), t, "FILLED", int(cum) if float(cum).is_integer() else cum,
                               _f(filled.get("average_price")), _ny_date(filled.get("last_transaction_at")),
                               "STOP" if filled is stop else "TARGET", placed)
        return BrokerOrder(aid, t, "WORKING" if live else "CANCELLED", placed_on=placed)

    def _send(self, name: str, args: dict):
        if self._call is None:
            raise BrokerError("no Robinhood session")
        try:
            res = self._call(name, args)
        except Exception as e:                       # NotAllowed, transport, API rejection
            self.sent.append({"tool": name, "args": args, "error": f"{type(e).__name__}: {e}"})
            raise BrokerError(f"{name}: {e}") from e
        self.sent.append({"tool": name, "args": args, "response": res})
        return res

    def submit(self, ticket: OrderTicket) -> BrokerOrder:
        existing = self.find(ticket.client_order_id)
        if existing is not None:
            return existing
        if ticket.qty <= 0 or int(ticket.qty) != ticket.qty:
            raise BrokerError("whole shares only")
        base = {"account_number": self.account_number, "symbol": ticket.ticker, "side": ticket.side.lower(),
                "quantity": str(int(ticket.qty)), "ref_id": ref_id(ticket.client_order_id),
                "time_in_force": "gtc" if ticket.time_in_force == "GTC" else "gfd"}
        if ticket.order_type == "OCO":
            if not (ticket.stop_price and ticket.take_profit_price and ticket.take_profit_price > ticket.stop_price):
                raise BrokerError("OCO needs stop < take-profit")
            name, args = "place_advanced_order", {**base, "market_hours": "regular_hours",
                                                  "stop_loss_stop_price": _px(ticket.stop_price),
                                                  "take_profit_limit_price": _px(ticket.take_profit_price)}
        elif ticket.order_type == "LIMIT":
            name, args = "place_equity_order", {**base, "type": "limit", "market_hours": "regular_hours",
                                                "limit_price": _px(ticket.limit_price)}
        elif ticket.order_type == "MARKET":
            name, args = "place_equity_order", {**base, "type": "market", "market_hours": "regular_hours"}
        elif ticket.order_type == "STOP":
            name, args = "place_equity_order", {**base, "type": "stop_market", "market_hours": "regular_hours",
                                                "stop_price": _px(ticket.stop_price)}
        else:
            raise BrokerError(f"order type {ticket.order_type} is not supported live")
        res = self._send(name, args)
        aid = _find_key(res, "approval_id")
        oid = _find_key(res, "id")
        bid = f"approval:{aid}" if aid else (str(oid) if oid else f"ref:{args['ref_id']}")
        self.kinds[bid] = "approval" if aid else ("advanced" if name == "place_advanced_order" else "equity")
        o = BrokerOrder(broker_order_id=bid, ticket=ticket, state="WORKING", placed_on=datetime.now(NY).date())
        self._orders.append(o)
        return o

    def cancel(self, broker_order_id: str) -> BrokerOrder:
        o = next((x for x in self._orders if x.broker_order_id == broker_order_id), None)
        if o is None:
            raise BrokerError(f"unknown order {broker_order_id}")
        kind = self.kinds.get(broker_order_id, "equity")
        if kind == "approval":
            self._send("decline_trade_approval", {"account_number": self.account_number,
                                                  "approval_id": broker_order_id.split(":", 1)[1]})
        elif kind == "advanced":
            self._send("cancel_advanced_order", {"account_number": self.account_number, "order_id": broker_order_id})
        else:
            self._send("cancel_equity_order", {"account_number": self.account_number, "order_id": broker_order_id})
        if o.state == "WORKING":
            o.state = "CANCELLED"
        return o

    def relinks(self) -> dict:
        """client id → the broker id the ledger should hold now (the real order once an approval
        was approved; the filled leg's equity order for an OCO, so sync_fills records the fill)."""
        return {o.ticket.client_order_id: o.broker_order_id for o in self._orders
                if o.ticket.client_order_id.startswith("wi-")
                and not o.broker_order_id.startswith(("approval:", "ref:"))}
