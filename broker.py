"""
broker.py — the broker interface the executor talks to (EXECUTOR_DESIGN.md §1.2), and
SimBroker, a simulated broker that fills orders against daily bars (phase 3).

Order tickets carry a client_order_id ("wi-<trade_orders.id>"). submit() is
IDEMPOTENT on it: submitting the same id again returns the existing order and
never creates a second one. That is what makes a crash between "the broker took
the order" and "we wrote it down" safe — on restart the executor looks the id up
before submitting anything.

SimBroker session model (one daily bar per ticker), in this order:
  1. MARKET orders fill at the open.
  2. LIMIT buys: open ≤ limit → fill at the open; else low ≤ limit → fill at the limit.
  3. OCO sells (stop + take-profit on the same shares): open ≤ stop → open; open ≥
     target → open; else a low touching the stop wins over a high touching the target
     (daily bars cannot order them, so assume the worse); else high ≥ target → target.
  4. STOP sells: open ≤ stop → open; low ≤ stop → stop.
  5. DAY orders still working at the close expire; GTC orders stay.
Costs: COST_BPS_PER_SIDE on every fill. Whole shares only.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Protocol

from backtest_engine import COST_BPS_PER_SIDE


REF_NAMESPACE = uuid.UUID("5d1f3c2e-8b7a-4f0e-9c61-2a4b7e9d0c13")


def ref_id(client_order_id: str) -> str:
    """The broker-side idempotency key for a client order id: the same "wi-<id>" always gives
    the same UUID, so a retry after a crash can never create a second order at Robinhood."""
    return str(uuid.uuid5(REF_NAMESPACE, client_order_id))


class BrokerError(Exception):
    """The broker refused or failed a request (network, validation, rate limit)."""


@dataclass
class OrderTicket:
    client_order_id: str
    ticker: str
    side: str                       # BUY | SELL
    order_type: str                 # LIMIT | MARKET | STOP | OCO
    qty: int
    time_in_force: str = "DAY"      # DAY | GTC
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    take_profit_price: Optional[float] = None   # OCO only


@dataclass
class BrokerOrder:
    broker_order_id: str
    ticket: OrderTicket
    state: str = "WORKING"          # WORKING | FILLED | CANCELLED | EXPIRED
    filled_qty: int = 0
    fill_price: Optional[float] = None
    fill_date: Optional[date] = None
    fill_leg: Optional[str] = None  # OCO: STOP | TARGET
    placed_on: Optional[date] = None


@dataclass
class AccountView:
    equity: float
    cash: float
    positions: dict                 # ticker → {"qty", "avg_cost", "last_price"}


class Broker(Protocol):
    def account(self) -> AccountView: ...
    def submit(self, ticket: OrderTicket) -> BrokerOrder: ...
    def cancel(self, broker_order_id: str) -> BrokerOrder: ...
    def find(self, client_order_id: str) -> Optional[BrokerOrder]: ...
    def orders(self) -> list[BrokerOrder]: ...


@dataclass
class SimBroker:
    cash: float
    today: Optional[date] = None
    positions: dict = field(default_factory=dict)       # ticker → {"qty", "avg_cost"}
    last_close: dict = field(default_factory=dict)       # ticker → price
    _orders: dict = field(default_factory=dict)          # broker_order_id → BrokerOrder
    _by_client: dict = field(default_factory=dict)       # client_order_id → broker_order_id
    _seq: int = 0
    fail_next: int = 0                                   # inject: the next N submits raise BrokerError
    submit_calls: int = 0                                # every submit() call, including duplicates
    fills_log: list = field(default_factory=list)

    # ── Broker interface ────────────────────────────────────────────────────
    def account(self) -> AccountView:
        pos = {t: {"qty": p["qty"], "avg_cost": p["avg_cost"], "last_price": self.last_close.get(t, p["avg_cost"])}
               for t, p in self.positions.items() if p["qty"] > 0}
        equity = self.cash + sum(p["qty"] * p["last_price"] for p in pos.values())
        return AccountView(equity=round(equity, 2), cash=round(self.cash, 2), positions=pos)

    def submit(self, ticket: OrderTicket) -> BrokerOrder:
        self.submit_calls += 1
        if ticket.client_order_id in self._by_client:                 # idempotent
            return self._orders[self._by_client[ticket.client_order_id]]
        if self.fail_next > 0:
            self.fail_next -= 1
            raise BrokerError("simulated broker failure")
        if ticket.qty <= 0 or int(ticket.qty) != ticket.qty:
            raise BrokerError("whole shares only")
        if ticket.order_type == "OCO" and not (ticket.stop_price and ticket.take_profit_price
                                               and ticket.take_profit_price > ticket.stop_price):
            raise BrokerError("OCO needs stop < take-profit")
        if ticket.side == "SELL":
            held = self.positions.get(ticket.ticker, {}).get("qty", 0)
            resting = sum(o.ticket.qty for o in self._orders.values()
                          if o.state == "WORKING" and o.ticket.side == "SELL" and o.ticket.ticker == ticket.ticker)
            if ticket.qty + resting > held:
                raise BrokerError("sell exceeds shares available")
        self._seq += 1
        o = BrokerOrder(broker_order_id=f"sim-{self._seq}", ticket=ticket, placed_on=self.today)
        self._orders[o.broker_order_id] = o
        self._by_client[ticket.client_order_id] = o.broker_order_id
        return o

    def cancel(self, broker_order_id: str) -> BrokerOrder:
        o = self._orders.get(broker_order_id)
        if o is None:
            raise BrokerError(f"unknown order {broker_order_id}")
        if o.state == "WORKING":
            o.state = "CANCELLED"
        return o

    def find(self, client_order_id: str) -> Optional[BrokerOrder]:
        bid = self._by_client.get(client_order_id)
        return self._orders.get(bid) if bid else None

    def orders(self) -> list[BrokerOrder]:
        return list(self._orders.values())

    # ── Simulation ──────────────────────────────────────────────────────────
    def _fill(self, o: BrokerOrder, price: float, d: date, leg: Optional[str] = None) -> None:
        t, qty = o.ticket.ticker, o.ticket.qty
        cost = price * qty * COST_BPS_PER_SIDE / 1e4
        p = self.positions.setdefault(t, {"qty": 0, "avg_cost": 0.0})
        if o.ticket.side == "BUY":
            if price * qty + cost > self.cash + 1e-9:
                o.state = "CANCELLED"                      # not enough cash: rejected at fill time
                return
            p["avg_cost"] = (p["avg_cost"] * p["qty"] + price * qty) / (p["qty"] + qty)
            p["qty"] += qty
            self.cash -= price * qty + cost
        else:
            p["qty"] -= qty
            self.cash += price * qty - cost
        o.state, o.filled_qty, o.fill_price, o.fill_date, o.fill_leg = "FILLED", qty, round(price, 4), d, leg
        self.fills_log.append({"date": d, "ticker": t, "side": o.ticket.side, "qty": qty, "price": round(price, 4),
                               "client_order_id": o.ticket.client_order_id, "leg": leg})

    def run_session(self, d: date, bars: dict) -> None:
        """bars: ticker → {"open","high","low","close"} for session d."""
        self.today = d
        working = [o for o in self._orders.values() if o.state == "WORKING"]
        for o in [o for o in working if o.ticket.order_type == "MARKET"]:
            b = bars.get(o.ticket.ticker)
            if b:
                self._fill(o, b["open"], d)
        for o in [o for o in working if o.state == "WORKING" and o.ticket.order_type == "LIMIT" and o.ticket.side == "BUY"]:
            b = bars.get(o.ticket.ticker)
            if not b:
                continue
            lim = o.ticket.limit_price
            if b["open"] <= lim:
                self._fill(o, b["open"], d)
            elif b["low"] <= lim:
                self._fill(o, lim, d)
        for o in [o for o in working if o.state == "WORKING" and o.ticket.order_type in ("OCO", "STOP")]:
            b = bars.get(o.ticket.ticker)
            if not b:
                continue
            stop, tp = o.ticket.stop_price, o.ticket.take_profit_price
            if b["open"] <= stop:
                self._fill(o, b["open"], d, "STOP")
            elif tp and b["open"] >= tp:
                self._fill(o, b["open"], d, "TARGET")
            elif b["low"] <= stop:
                self._fill(o, stop, d, "STOP")
            elif tp and b["high"] >= tp:
                self._fill(o, tp, d, "TARGET")
        for t, b in bars.items():
            self.last_close[t] = b["close"]
        for o in self._orders.values():
            if o.state == "WORKING" and o.ticket.time_in_force == "DAY":
                o.state = "EXPIRED"
