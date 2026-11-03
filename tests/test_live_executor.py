"""Phase 5: the live switch, the order-sending guard, the live Robinhood adapter (with a fake
session: nothing here reaches Robinhood), ref_id linking in sync_fills, and the write-through ledger."""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from broker import BrokerError, OrderTicket, ref_id  # noqa: E402
from exec_limits import Mandate  # noqa: E402
from executor import Executor, MemoryLedger  # noqa: E402
from rh_client import NotAllowed  # noqa: E402
from rh_trader import guard_trade_call  # noqa: E402
from robinhood_broker import LiveRobinhoodBroker  # noqa: E402
from sync_fills import plan_sync  # noqa: E402

ACCT = "900000001"                     # tests/conftest.py
S = [date(2026, 10, d) for d in (13, 14, 15, 16, 19, 20)]
D = S[-1]
PF = {"data": {"total_value": "5000.00", "cash": "5000.00"}}
NOPOS = {"data": {"positions": []}}


class FakeRH:
    """Stands in for the session: records calls, returns a canned approval response."""

    def __init__(self, fail=False):
        self.calls, self.fail, self.n = [], fail, 0

    def __call__(self, name, args):
        self.calls.append((name, args))
        if self.fail:
            raise RuntimeError("upstream 400")
        self.n += 1
        return {"data": {"approval": {"approval_id": f"ap{self.n}", "state": "pending"}}}


def leg(id, type_, state="confirmed", price=None, stop=None, cum="0", trigger="immediate"):
    return {"id": id, "symbol": "NVDA", "side": "sell", "type": type_, "state": state, "quantity": "5",
            "cumulative_quantity": cum, "price": price, "stop_price": stop, "average_price": stop if cum != "0" else None,
            "time_in_force": "gtc", "trigger": trigger, "created_at": "2026-10-16T14:00:00Z",
            "last_transaction_at": "2026-10-19T15:00:00Z"}


# ── the switch and the guard ──────────────────────────────────────────────────

def test_order_tools_refused_unless_switch_on_account_pinned_and_approval_queue_on(monkeypatch):
    args = {"account_number": ACCT, "symbol": "NVDA"}
    monkeypatch.delenv("EXECUTOR_LIVE", raising=False)
    with pytest.raises(NotAllowed, match="switch is off"):
        guard_trade_call("place_equity_order", args, approval_confirmed=True)
    monkeypatch.setenv("EXECUTOR_LIVE", "true")                       # only exactly "1" counts
    with pytest.raises(NotAllowed, match="switch is off"):
        guard_trade_call("place_equity_order", args, approval_confirmed=True)
    monkeypatch.setenv("EXECUTOR_LIVE", "1")
    with pytest.raises(NotAllowed, match="approval queue"):
        guard_trade_call("place_equity_order", args, approval_confirmed=False)
    with pytest.raises(NotAllowed, match="agentic"):
        guard_trade_call("place_equity_order", {**args, "account_number": "900000002"}, approval_confirmed=True)
    with pytest.raises(NotAllowed):
        guard_trade_call("exercise_option", args, approval_confirmed=True)
    guard_trade_call("place_equity_order", args, approval_confirmed=True)
    guard_trade_call("get_advanced_orders", {"account_number": ACCT}, approval_confirmed=False)   # reads always OK


def test_run_executor_does_nothing_with_the_switch_off(monkeypatch, capsys):
    import run_executor
    monkeypatch.delenv("EXECUTOR_LIVE", raising=False)
    assert run_executor.run() == 0
    assert "LIVE SWITCH OFF" in capsys.readouterr().out


def test_jobs_switch(monkeypatch):
    import jobs
    monkeypatch.setenv("EXECUTOR_LIVE", "0")
    assert not jobs.live_executor_on()
    monkeypatch.setenv("EXECUTOR_LIVE", "1")
    assert jobs.live_executor_on()


# ── submit / cancel ───────────────────────────────────────────────────────────

def test_submit_maps_tickets_with_a_stable_ref_id_and_is_idempotent():
    rh = FakeRH()
    b = LiveRobinhoodBroker(PF, NOPOS, {"data": {"orders": []}}, [], {}, S[0], call=rh)
    t = OrderTicket("wi-7", "NVDA", "BUY", "LIMIT", 5, "DAY", limit_price=100.25)
    o1, o2 = b.submit(t), b.submit(t)
    assert o1 is o2 and len(rh.calls) == 1 and o1.broker_order_id == "approval:ap1"
    name, args = rh.calls[0]
    assert name == "place_equity_order"
    assert args == {"account_number": ACCT, "symbol": "NVDA", "side": "buy", "quantity": "5", "type": "limit",
                    "limit_price": "100.25", "time_in_force": "gfd", "market_hours": "regular_hours",
                    "ref_id": ref_id("wi-7")}
    assert ref_id("wi-7") == ref_id("wi-7") != ref_id("wi-8")

    b.submit(OrderTicket("wi-8", "NVDA", "SELL", "OCO", 5, "GTC", stop_price=96.0, take_profit_price=108.0))
    name, args = rh.calls[1]
    assert name == "place_advanced_order" and args["stop_loss_stop_price"] == "96.00"
    assert args["take_profit_limit_price"] == "108.00" and args["time_in_force"] == "gtc"

    b.cancel("approval:ap1")                                            # a pending approval is declined
    assert rh.calls[2] == ("decline_trade_approval", {"account_number": ACCT, "approval_id": "ap1"})


def test_submit_refuses_bad_tickets_and_turns_failures_into_broker_errors():
    b = LiveRobinhoodBroker(PF, NOPOS, {"data": {"orders": []}}, [], {}, S[0], call=FakeRH())
    with pytest.raises(BrokerError, match="tick"):
        b.submit(OrderTicket("wi-1", "NVDA", "BUY", "LIMIT", 5, limit_price=100.123))
    with pytest.raises(BrokerError, match="whole shares"):
        b.submit(OrderTicket("wi-2", "NVDA", "BUY", "LIMIT", 0.5, limit_price=100.0))
    bad = LiveRobinhoodBroker(PF, NOPOS, {"data": {"orders": []}}, [], {}, S[0], call=FakeRH(fail=True))
    with pytest.raises(BrokerError, match="upstream 400"):
        bad.submit(OrderTicket("wi-3", "NVDA", "BUY", "LIMIT", 5, limit_price=100.0))
    assert "error" in bad.sent[0] and bad.find("wi-3") is None
    with pytest.raises(BrokerError, match="no Robinhood session"):
        LiveRobinhoodBroker(PF, NOPOS, {"data": {"orders": []}}, [], {}, S[0]).submit(
            OrderTicket("wi-4", "NVDA", "BUY", "LIMIT", 5, limit_price=100.0))


# ── reading our own orders back ───────────────────────────────────────────────

def test_oco_groups_replace_their_legs_and_are_recognised_by_ref_id():
    pos = {"data": {"positions": [{"symbol": "NVDA", "quantity": "5", "average_buy_price": "100"}]}}
    legs = [leg("tp1", "limit", price="108.00"), leg("sl1", "market", stop="96.00", trigger="stop")]
    adv = {"data": {"advanced_orders": [{"id": "adv1", "state": "active", "ref_id": ref_id("wi-9").upper(),
                                         "created_at": "2026-10-16T14:00:00Z",
                                         "advanced_order_links": [{"order_id": l["id"], "order": l} for l in legs]}]}}
    b = LiveRobinhoodBroker(PF, pos, {"data": {"orders": legs}}, [], {}, S[2], advanced=adv, cids=["wi-9"])
    (o,) = b.orders()
    assert o.broker_order_id == "adv1" and o.ticket.client_order_id == "wi-9" and o.state == "WORKING"
    assert (o.ticket.order_type, o.ticket.stop_price, o.ticket.take_profit_price) == ("OCO", 96.0, 108.0)
    assert b.kinds["adv1"] == "advanced"

    rh = FakeRH()
    b._call = rh
    b.cancel("adv1")
    assert rh.calls == [("cancel_advanced_order", {"account_number": ACCT, "order_id": "adv1"})]


def test_a_filled_oco_carries_the_filled_legs_id():
    legs = [leg("tp1", "limit", state="cancelled", price="108.00"),
            leg("sl1", "market", state="filled", stop="96.00", cum="5", trigger="stop")]
    adv = {"data": {"advanced_orders": [{"id": "adv1", "state": "filled", "ref_id": ref_id("wi-9"),
                                         "advanced_order_links": [{"order_id": l["id"], "order": l} for l in legs]}]}}
    b = LiveRobinhoodBroker(PF, NOPOS, {"data": {"orders": legs}}, [], {}, S[2], advanced=adv, cids=["wi-9"])
    (o,) = b.orders()
    assert (o.broker_order_id, o.state, o.fill_leg, o.filled_qty) == ("sl1", "FILLED", "STOP", 5)
    assert b.relinks() == {"wi-9": "sl1"}


def test_approved_orders_are_matched_by_ref_id_and_unknown_approvals_stay_untracked():
    mine = {"id": "eq1", "ref_id": ref_id("wi-5"), "symbol": "NVDA", "side": "buy", "type": "limit", "state": "confirmed",
            "quantity": "5", "cumulative_quantity": "0", "price": "100.00", "time_in_force": "gfd",
            "created_at": "2026-10-20T13:25:00Z"}
    approvals = {"data": {"approvals": [{"approval_id": "apX", "order": {"symbol": "AMD", "side": "buy", "quantity": "1"}}]}}
    b = LiveRobinhoodBroker(PF, NOPOS, {"data": {"orders": [mine]}}, [], {"approval:ap1": 5}, S[2],
                            approvals=approvals, cids=["wi-5"])
    by = {o.broker_order_id: o for o in b.orders()}
    assert by["eq1"].ticket.client_order_id == "wi-5"
    assert by["approval:apX"].ticket.client_order_id == "rh-approval:apX"      # not ours → reconcile halts
    assert b.relinks() == {"wi-5": "eq1"}


# ── the executor end to end on the live adapter ───────────────────────────────

def test_executor_sends_an_approved_entry_once_and_halts_on_an_unknown_approval(tmp_path):
    def ledger():
        L = MemoryLedger()
        L.mandates[D] = Mandate(D, 2, 100.0, frozenset({"NVDA"}),
                                expires_at=datetime(2026, 10, 20, 20, tzinfo=timezone.utc))
        L.add_order({"plan_date": D.isoformat(), "report_date": D, "ticker": "NVDA", "side": "BUY", "intent": "ENTRY",
                     "order_type": "LIMIT", "qty": 5, "limit_price": 100.0, "stop_price": 96.0, "target_price": 108.0,
                     "horizon_days": 5, "risk_dollars": 20.0, "rank": 0})
        return L

    rh = FakeRH()
    L = ledger()
    b = LiveRobinhoodBroker(PF, NOPOS, {"data": {"orders": []}}, [], {}, S[2], cids=["wi-1"], call=rh)
    ex = Executor(b, L, S, stop_file=str(tmp_path / "STOP"))
    now = datetime(2026, 10, 20, 13, 25, tzinfo=timezone.utc)
    assert ex.pre_open(D, now)["status"] == "OK"
    ex.pre_open(D, now)
    assert [c[0] for c in rh.calls] == ["place_equity_order"]
    assert L.orders[1]["status"] == "PLACED" and L.orders[1]["broker_order_id"] == "approval:ap1"

    stray = {"data": {"approvals": [{"approval_id": "apZ", "order": {"symbol": "TSLA", "side": "buy", "quantity": "3"}}]}}
    rh2 = FakeRH()
    b2 = LiveRobinhoodBroker(PF, NOPOS, {"data": {"orders": []}}, [], {}, S[2], approvals=stray, cids=["wi-1"], call=rh2)
    res = Executor(b2, ledger(), S, stop_file=str(tmp_path / "STOP")).pre_open(D, now)
    assert res["status"] == "HALTED" and rh2.calls == []


# ── sync_fills and the write-through ledger ───────────────────────────────────

def test_sync_fills_links_executor_orders_by_ref_id():
    ours = [{"id": 5, "ticker": "NVDA", "side": "BUY", "qty": 5, "order_type": "LIMIT", "limit_price": 100.0,
             "plan_date": "2026-10-20", "status": "PLACED", "broker_order_id": "approval:ap1", "time_in_force": "DAY"}]
    broker = [{"id": "eq1", "ref_id": ref_id("wi-5").upper(), "symbol": "NVDA", "side": "buy", "type": "limit",
               "state": "filled", "quantity": "5", "cumulative_quantity": "5", "price": "100.00",
               "average_price": "99.90", "created_at": "2026-10-20T13:30:00Z",
               "last_transaction_at": "2026-10-20T13:31:00Z"}]
    plan = plan_sync(ours, broker, {}, D)
    assert plan["link"] == [(5, "eq1")] and plan["status"][5] == "FILLED"
    assert plan["fills"][0]["broker_order_id"] == "eq1" and plan["untracked"] == []
    assert ours[0]["broker_order_id"] == "approval:ap1"                 # pure: the input is untouched


class FakeTable:
    def __init__(self, db, name):
        self.db, self.name, self.op = db, name, None

    def insert(self, rec):
        self.op = ("insert", rec)
        return self

    def update(self, rec):
        self.op = ("update", rec)
        return self

    def eq(self, col, val):
        self.op = (*self.op, val)
        return self

    def execute(self):
        self.db.log.append((self.name, *self.op))
        if self.op[0] == "insert":
            self.db.n += 1
            return type("R", (), {"data": [{**self.op[1], "id": 100 + self.db.n}]})
        return type("R", (), {"data": []})


class FakeDB:
    def __init__(self):
        self.log, self.n = [], 0

    def table(self, name):
        return FakeTable(self, name)


def test_supabase_ledger_writes_through_only_what_it_owns():
    from run_executor import SupabaseLedger
    db = FakeDB()
    L = SupabaseLedger(sb=db, plan_id="executor-2026-10-20")
    row = L.add_order({"plan_date": "2026-10-20", "ticker": "NVDA", "side": "SELL", "intent": "PROTECT_STOP",
                       "order_type": "OCO", "qty": 5, "stop_price": 96.0, "target_price": 108.0,
                       "time_in_force": "GTC", "report_date": D})
    assert row["id"] == 101 and L.orders[101]["intent"] == "PROTECT_STOP"
    assert db.log[0][0:2] == ("trade_orders", "insert") and db.log[0][2]["report_date"] == "2026-10-20"
    L.update_order(101, status="PLACED", broker_order_id="approval:ap1")
    L.update_order(101, status="FILLED", fill_intent="EXIT_TARGET")         # sync_fills owns fills: not written
    L.add_event(D, row, "SUBMIT", True, "approval:ap1")
    assert [(t, op) for (t, op, *_) in db.log] == [("trade_orders", "insert"), ("trade_orders", "update"),
                                                  ("exec_events", "insert")]
    assert db.log[1][2]["status"] == "PLACED" and db.log[1][3] == 101
    assert db.log[2][2]["client_order_id"] == "wi-101"
