"""
executor.py — the order executor (EXECUTOR_DESIGN.md §1.4). Deterministic: no model calls.

Phase 3 runs it ONLY against broker.SimBroker with a MemoryLedger (shaped like
trade_orders / trade_fills / exec_events / trading_mandates / trading_controls).
It is the user's program: in later phases the user starts it, under the user's
credentials, against Robinhood's approval queue.

Each session:
  pre_open(d)   1. sync fills from the broker into the ledger
                2. RECONCILE ledger positions with broker positions; any mismatch or
                   an order the ledger doesn't know → HALT (nothing else happens)
                3. STOP file → do nothing; controls.halted → exits only
                4. exits first: a position past its horizon → cancel its OCO, sell at
                   the open (EXIT_TIME); a position without a covering OCO, even one
                   filled today → place one at the plan stop / target (PROTECT_STOP)
                5. entries: APPROVED entry orders for today, best first, each through
                   exec_limits.check_submission (guard + ceilings + mandate)
  post_close(d) sync fills, record expiries/cancels.

Every submit goes through _submit():
  • client_order_id = "wi-<order id>"; the broker is asked for it FIRST, so a crash
    after the broker accepted an order but before the ledger recorded it is
    recovered (linked), never re-sent
  • a failed attempt is logged; the retry must be a RESUBMIT and counts against
    MAX_RESUBMITS; 3 broker errors in a row stop all submissions for the day
"""

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Optional

from broker import BrokerError, OrderTicket
from exec_limits import ENTRY, ExecState, Mandate, check_submission
from exec_state import build_exec_state
from trade_guard import GuardContext

STOP_FILE = os.path.join(".trading", "STOP")
PROTECTIVE = ("OCO", "STOP", "STOP_LIMIT")
PLAN_EXITS = ("EXIT_SIGNAL", "EXIT_STOP", "EXIT_TARGET")
MAX_CONSECUTIVE_BROKER_ERRORS = 3


class SimulatedCrash(Exception):
    """Raised at an injected crash point (tests and replay only)."""


# ── Ledger (in memory; the Supabase ledger in phase 4 has the same methods) ──────

@dataclass
class MemoryLedger:
    orders: dict = field(default_factory=dict)          # id → trade_orders-like row
    fills: list = field(default_factory=list)
    events: list = field(default_factory=list)
    mandates: dict = field(default_factory=dict)        # date → Mandate
    halted: bool = False
    halt_reason: Optional[str] = None
    equity_peak: Optional[float] = None
    alerts: list = field(default_factory=list)
    submitted: list = field(default_factory=list)       # (date, order id, ticker, intent, kind, ok): survives restarts
    refusals: list = field(default_factory=list)        # (date, order id, ticker, intent, reasons)
    broker_errors: dict = field(default_factory=dict)   # date → consecutive broker errors that day (survives restarts)
    _next_id: int = 1
    synced: set = field(default_factory=set)            # broker_order_ids already turned into fills

    def add_order(self, row: dict) -> dict:
        row = {"status": "APPROVED", **row, "id": self._next_id}
        self.orders[self._next_id] = row
        self._next_id += 1
        return row

    def update_order(self, oid: int, **kw) -> None:
        self.orders[oid].update(kw)

    def add_event(self, d: date, order: dict, kind: str, ok: bool, broker_order_id=None, detail=None) -> None:
        self.events.append({"order_id": order["id"], "kind": kind, "ticker": order["ticker"],
                            "intent": order["intent"], "risk_dollars": order.get("risk_dollars"),
                            "client_order_id": f"wi-{order['id']}", "broker_order_id": broker_order_id,
                            "ok": ok, "detail": detail, "trade_date": d.isoformat()})

    def positions(self) -> dict:
        """ticker → qty, from the ledger's own fills."""
        out: dict = {}
        for f in self.fills:
            out[f["ticker"]] = out.get(f["ticker"], 0) + (f["qty"] if f["side"] == "BUY" else -f["qty"])
        return {t: q for t, q in out.items() if q != 0}

    def managed(self) -> dict:
        """ticker → plan levels and opening date of the CURRENT lot."""
        out: dict = {}
        for f in sorted(self.fills, key=lambda f: (f["trade_date"], f["seq"])):
            t = f["ticker"]
            if f["side"] == "BUY":
                o = self.orders.get(f["order_id"]) or {}
                if t not in out:
                    out[t] = {"opened_date": date.fromisoformat(f["trade_date"]), "qty": 0,
                              "stop_price": o.get("stop_price"), "target_price": o.get("target_price"),
                              "horizon_days": o.get("horizon_days"), "entry_order_id": f["order_id"]}
                out[t]["qty"] += f["qty"]
            else:
                if t in out:
                    out[t]["qty"] -= f["qty"]
                    if out[t]["qty"] <= 0:
                        del out[t]
        return out


@dataclass
class Executor:
    broker: object
    ledger: MemoryLedger
    sessions: list                                    # the trading calendar (dates, ascending)
    crash_at: Optional[str] = None                    # "before_submit" | "after_submit" (tests/replay)
    stop_file: str = STOP_FILE
    _fill_seq: int = 0

    # ── ledger ↔ broker ──────────────────────────────────────────────────────
    def sync_fills(self, d: date) -> None:
        for bo in self.broker.orders():
            oid = _oid(bo.ticket.client_order_id)
            if oid is None or oid not in self.ledger.orders:
                continue                                       # reconcile() reports it
            row = self.ledger.orders[oid]
            if bo.state == "FILLED" and bo.broker_order_id not in self.ledger.synced:
                self._fill_seq += 1
                self.ledger.fills.append({"order_id": oid, "ticker": bo.ticket.ticker, "side": bo.ticket.side,
                                          "qty": bo.filled_qty, "price": bo.fill_price,
                                          "trade_date": bo.fill_date.isoformat(), "seq": self._fill_seq,
                                          "filled_at": f"{bo.fill_date.isoformat()}T{self._fill_seq:06d}",
                                          "leg": bo.fill_leg})
                self.ledger.synced.add(bo.broker_order_id)
                exit_intent = row["intent"]
                if row["intent"] == "PROTECT_STOP" and bo.fill_leg == "TARGET":
                    exit_intent = "EXIT_TARGET"                   # took profit, not a stop-out
                self.ledger.update_order(oid, status="FILLED", fill_intent=exit_intent)
                if exit_intent != row["intent"]:
                    self.ledger.fills[-1]["exit_intent"] = exit_intent
            elif bo.state in ("EXPIRED", "CANCELLED") and row["status"] in ("PLACED", "APPROVED"):
                self.ledger.update_order(oid, status=bo.state)

    def reconcile(self) -> list[str]:
        problems = []
        broker_pos = {t: p["qty"] for t, p in self.broker.account().positions.items()}
        ledger_pos = self.ledger.positions()
        for t in sorted(set(broker_pos) | set(ledger_pos)):
            if broker_pos.get(t, 0) != ledger_pos.get(t, 0):
                problems.append(f"{t}: broker {broker_pos.get(t, 0)} vs ledger {ledger_pos.get(t, 0)}")
        for bo in self.broker.orders():
            oid = _oid(bo.ticket.client_order_id)
            if bo.state in ("WORKING", "FILLED") and (oid is None or oid not in self.ledger.orders):
                problems.append(f"untracked broker order {bo.broker_order_id} ({bo.ticket.ticker})")
        return problems

    # ── state for the gate ───────────────────────────────────────────────────
    def _guard_ctx(self, d: date) -> GuardContext:
        acct = self.broker.account()
        today = d.isoformat()
        fills_today = [f for f in self.ledger.fills if f["trade_date"] == today]
        managed = {t: {"stop_price": m["stop_price"], "opened_date": m["opened_date"]}
                   for t, m in self.ledger.managed().items()}
        working = [{"ticker": o.ticket.ticker, "side": o.ticket.side, "type": o.ticket.order_type, "qty": o.ticket.qty}
                   for o in self.broker.orders() if o.state == "WORKING"]
        if self.ledger.equity_peak is None or acct.equity > self.ledger.equity_peak:
            self.ledger.equity_peak = acct.equity
        return GuardContext(today=d, equity=acct.equity, buying_power=acct.cash, positions=acct.positions,
                            bought_today={f["ticker"] for f in fills_today if f["side"] == "BUY"},
                            sold_today={f["ticker"] for f in fills_today if f["side"] == "SELL"},
                            open_orders=working, managed=managed, universe=None,
                            halted=self.ledger.halted, halt_reason=self.ledger.halt_reason,
                            equity_peak=self.ledger.equity_peak)

    def _exec_state(self, d: date) -> ExecState:
        i = self.sessions.index(d)
        window = self.sessions[max(0, i - 4): i + 1]
        fills = [{**f, "filled_at": f["filled_at"]} for f in self.ledger.fills]
        orders = {oid: {**o, "intent": o.get("fill_intent") or o["intent"]} for oid, o in self.ledger.orders.items()}
        return build_exec_state(d, window, self.ledger.events, fills, orders)

    # ── submit through the gate ──────────────────────────────────────────────
    def _submit(self, d: date, now: datetime, row: dict, ticket: OrderTicket) -> bool:
        cid = f"wi-{row['id']}"
        existing = self.broker.find(cid)
        if existing is not None:                                   # crash recovery: link, never re-send
            if not row.get("broker_order_id"):
                self.ledger.update_order(row["id"], status="PLACED", broker_order_id=existing.broker_order_id)
                if not any(e["order_id"] == row["id"] and e["ok"] for e in self.ledger.events):
                    self.ledger.add_event(d, row, "SUBMIT", True, existing.broker_order_id, "recovered after restart")
            return True
        if self.ledger.broker_errors.get(d, 0) >= MAX_CONSECUTIVE_BROKER_ERRORS:
            self.ledger.refusals.append((d, row["id"], row["ticker"], row["intent"], ["BROKER_ERRORS_TODAY"]))
            return False

        prior = [e for e in self.ledger.events if e["order_id"] == row["id"] and e["kind"] in ("SUBMIT", "RESUBMIT")]
        kind = "RESUBMIT" if prior else "SUBMIT"
        reasons = check_submission(row, self._guard_ctx(d), self._exec_state(d), self.ledger.mandates.get(d), now,
                                   kind=kind)
        if reasons:
            self.ledger.refusals.append((d, row["id"], row["ticker"], row["intent"], reasons))
            return False

        if self.crash_at == "before_submit":
            self.crash_at = None
            raise SimulatedCrash("before_submit")
        try:
            bo = self.broker.submit(ticket)
        except BrokerError as e:
            self.ledger.add_event(d, row, kind, False, None, str(e))
            self.ledger.submitted.append((d, row["id"], row["ticker"], row["intent"], kind, False))
            n = self.ledger.broker_errors[d] = self.ledger.broker_errors.get(d, 0) + 1
            if n >= MAX_CONSECUTIVE_BROKER_ERRORS:
                self.ledger.alerts.append(f"{d}: {n} broker errors in a row; submissions stopped for the day")
            return False
        if self.crash_at == "after_submit":
            self.crash_at = None
            raise SimulatedCrash("after_submit")
        self.ledger.broker_errors[d] = 0
        self.ledger.add_event(d, row, kind, True, bo.broker_order_id)
        self.ledger.update_order(row["id"], status="PLACED", broker_order_id=bo.broker_order_id)
        self.ledger.submitted.append((d, row["id"], row["ticker"], row["intent"], kind, True))
        return True

    def _pending(self, ticker: str, intent: str, d: date) -> Optional[dict]:
        """Today's not-yet-placed exit of this kind for the ticker: retried (RESUBMIT, counted
        against the retry limit) instead of being replaced by a fresh order every pass."""
        for o in self.ledger.orders.values():
            if (o["ticker"] == ticker and o["intent"] == intent and o["plan_date"] == d.isoformat()
                    and o["status"] == "APPROVED"):
                return o
        return None

    def _cancel(self, d: date, row: dict, broker_order_id: str) -> None:
        self.broker.cancel(broker_order_id)
        self.ledger.add_event(d, row, "CANCEL", True, broker_order_id)
        self.ledger.update_order(row["id"], status="CANCELLED")

    # ── the session ──────────────────────────────────────────────────────────
    def pre_open(self, d: date, now: Optional[datetime] = None) -> dict:
        now = now or datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc).replace(hour=13, minute=35)
        self.sync_fills(d)
        problems = self.reconcile()
        if problems:
            self.ledger.halted, self.ledger.halt_reason = True, "reconcile: " + "; ".join(problems)
            self.ledger.alerts.append(f"{d}: HALT — {self.ledger.halt_reason}")
            return {"status": "HALTED", "problems": problems}
        if os.path.exists(self.stop_file):
            return {"status": "STOP_FILE"}

        # Exits first.
        managed = self.ledger.managed()
        # A resting OCO or plain stop (e.g. one the user placed by hand) already protects a position.
        working = {o.ticket.ticker: o for o in self.broker.orders()
                   if o.state == "WORKING" and o.ticket.side == "SELL" and o.ticket.order_type in PROTECTIVE}
        exiting = {o.ticket.ticker for o in self.broker.orders()
                   if o.state == "WORKING" and o.ticket.side == "SELL" and o.ticket.order_type not in PROTECTIVE}
        # Approved plan exits (analyst SELL, close under the stop, target hit): cancel the
        # protective order, then sell. Executor-made exits are handled below.
        for row in sorted((o for o in self.ledger.orders.values()
                           if o["intent"] in PLAN_EXITS and o["status"] == "APPROVED" and o["plan_date"] == d.isoformat()),
                          key=lambda o: o["id"]):
            t = row["ticker"]
            if t in exiting or t not in managed:
                continue
            prot = working.get(t)
            if prot is not None:
                prow = self.ledger.orders.get(_oid(prot.ticket.client_order_id)) or {"id": None, "ticker": t,
                                                                                    "intent": "PROTECT_STOP"}
                self.broker.cancel(prot.broker_order_id)
                if prow.get("id") is not None:
                    self.ledger.add_event(d, prow, "CANCEL", True, prot.broker_order_id)
                    self.ledger.update_order(prow["id"], status="CANCELLED")
                del working[t]
            qty = int(managed[t]["qty"])
            if self._submit(d, now, {**row, "qty": qty}, OrderTicket(f"wi-{row['id']}", t, "SELL", "MARKET", qty)):
                exiting.add(t)

        last = {t: p.get("last_price") for t, p in self.broker.account().positions.items()}
        for t, m in sorted(managed.items()):
            if t in exiting:
                continue
            # Sessions COMPLETED since the fill day, as trade_plan counts them: the backtest holds
            # through the close of the horizon-th session, and the nearest live exit is the next open.
            held = (self.sessions.index(d) - self.sessions.index(m["opened_date"]) - 1
                    if m["opened_date"] in self.sessions else 0)
            if m.get("horizon_days") and held >= int(m["horizon_days"]):
                oco = working.get(t)
                if oco is not None:
                    self._cancel(d, self.ledger.orders[_oid(oco.ticket.client_order_id)], oco.broker_order_id)
                row = self._pending(t, "EXIT_TIME", d) or self.ledger.add_order(
                    {"plan_date": d.isoformat(), "ticker": t, "side": "SELL", "intent": "EXIT_TIME",
                     "order_type": "MARKET", "qty": m["qty"], "report_date": d})
                self._submit(d, now, row, OrderTicket(f"wi-{row['id']}", t, "SELL", "MARKET", m["qty"]))
            elif t not in working and m.get("stop_price") and last.get(t) is not None and last[t] <= float(m["stop_price"]):
                # Already through the stop with nothing resting: a sell stop above the market is
                # rejected (or fires at once), so exit at the open, as trade_plan's EXIT_STOP does.
                row = self._pending(t, "EXIT_STOP", d) or self.ledger.add_order(
                    {"plan_date": d.isoformat(), "ticker": t, "side": "SELL", "intent": "EXIT_STOP",
                     "order_type": "MARKET", "qty": m["qty"], "report_date": d})
                self._submit(d, now, row, OrderTicket(f"wi-{row['id']}", t, "SELL", "MARKET", m["qty"]))
            elif t not in working and m.get("stop_price") and m.get("target_price"):
                row = self._pending(t, "PROTECT_STOP", d) or self.ledger.add_order(
                    {"plan_date": d.isoformat(), "ticker": t, "side": "SELL", "intent": "PROTECT_STOP",
                     "order_type": "OCO", "qty": m["qty"], "stop_price": m["stop_price"],
                     "target_price": m["target_price"], "time_in_force": "GTC", "report_date": d})
                self._submit(d, now, row, OrderTicket(f"wi-{row['id']}", t, "SELL", "OCO", m["qty"], "GTC",
                                                      stop_price=m["stop_price"], take_profit_price=m["target_price"]))

        # Then entries, best first.
        entries = sorted([o for o in self.ledger.orders.values()
                          if o["intent"] == ENTRY and o["status"] == "APPROVED" and o["plan_date"] == d.isoformat()],
                         key=lambda o: o.get("rank", 99))
        for row in entries:
            self._submit(d, now, row, OrderTicket(f"wi-{row['id']}", row["ticker"], "BUY", "LIMIT", int(row["qty"]),
                                                  "DAY", limit_price=row["limit_price"]))
        return {"status": "OK"}

    def post_close(self, d: date) -> None:
        self.sync_fills(d)


def _oid(client_order_id: str) -> Optional[int]:
    if client_order_id and client_order_id.startswith("wi-"):
        try:
            return int(client_order_id[3:])
        except ValueError:
            return None
    return None
