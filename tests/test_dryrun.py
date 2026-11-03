"""Phase 4: the read-only Robinhood adapter, the dry-run wrapper and the comparison."""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from broker import BrokerError, OrderTicket  # noqa: E402
from compare_dryrun import compare  # noqa: E402
from robinhood_broker import DryRunBroker, RobinhoodBroker  # noqa: E402

D = date(2026, 10, 15)
PF = {"data": {"total_value": "500.00", "cash": "300.00"}}
POS = {"data": {"positions": [{"symbol": "PD", "quantity": "2.000000", "average_buy_price": "16.66"},
                              {"symbol": "NTNX", "quantity": "0.000000"}]}}
Q = [{"data": {"results": [{"quote": {"symbol": "PD", "last_trade_price": "16.90"}, "close": {}}]}}]


def rh_order(id, symbol, side="buy", type_="limit", state="filled", qty="2", cum="2", price="17.07", created="2026-10-14T13:28:53Z", **kw):
    return {"id": id, "symbol": symbol, "side": side, "type": type_, "state": state, "quantity": qty,
            "cumulative_quantity": cum, "price": price, "average_price": "16.66", "time_in_force": "gfd",
            "trigger": "immediate", "created_at": created, "last_transaction_at": created, **kw}


def test_robinhood_broker_is_read_only_and_maps_orders():
    orders = {"data": {"orders": [
        rh_order("a", "PD"),                                                       # untracked
        rh_order("b", "GTLB", state="confirmed", cum="0", qty="1", price="54.10"),  # linked, working
        rh_order("c", "PD", side="sell", type_="market", state="confirmed", cum="0", trigger="stop", stop_price="15.80"),
        rh_order("old", "X", created="2026-09-01T13:00:00Z"),                      # older than the window
    ]}}
    b = RobinhoodBroker(PF, POS, orders, Q, {"b": 6}, since=date(2026, 10, 13))
    acct = b.account()
    assert acct.equity == 500.0 and acct.positions == {"PD": {"qty": 2, "avg_cost": 16.66, "last_price": 16.9}}
    by = {o.broker_order_id: o for o in b.orders()}
    assert set(by) == {"a", "b", "c"}
    assert by["a"].ticket.client_order_id == "rh-a" and by["a"].state == "FILLED"
    assert by["b"].ticket.client_order_id == "wi-6" and by["b"].state == "WORKING" and b.find("wi-6") is by["b"]
    assert by["c"].ticket.order_type == "STOP" and by["c"].ticket.stop_price == 15.8
    with pytest.raises(BrokerError):
        b.submit(OrderTicket("wi-9", "PD", "BUY", "LIMIT", 1, limit_price=16.0))
    with pytest.raises(BrokerError):
        b.cancel("b")


def test_dryrun_records_and_shows_would_orders_without_touching_the_account():
    b = RobinhoodBroker(PF, POS, {"data": {"orders": [rh_order("c", "PD", side="sell", state="confirmed", cum="0",
                                                               trigger="stop", stop_price="15.8")]}}, Q, {}, D)
    dry = DryRunBroker(b)
    t = OrderTicket("wi-1", "GTLB", "BUY", "LIMIT", 1, limit_price=54.1)
    o1, o2 = dry.submit(t), dry.submit(t)
    assert o1 is o2 and len(dry.would) == 1 and dry.find("wi-1") is o1
    assert any(o.ticket.client_order_id == "wi-1" and o.state == "WORKING" for o in dry.orders())
    dry.cancel("c")
    assert [w["action"] for w in dry.would] == ["SUBMIT", "CANCEL"]
    assert next(o for o in dry.orders() if o.broker_order_id == "c").state == "CANCELLED"
    assert next(o for o in b.orders() if o.broker_order_id == "c").state == "WORKING"   # the real view is untouched


def test_compare_labels():
    run = {"status": "OK", "mandate_source": "DASHBOARD",
           "would": [{"action": "SUBMIT", "ticker": "GTLB", "side": "BUY", "type": "LIMIT", "qty": 1, "limit": 54.1},
                     {"action": "SUBMIT", "ticker": "DT", "side": "BUY", "type": "LIMIT", "qty": 1, "limit": 63.6}],
           "refusals": [{"ticker": "NTNX", "reasons": ["MAX_ENTRIES_TODAY"]}]}
    todays = [{"ticker": "GTLB", "side": "BUY", "broker_order_id": "b1", "status": "PLACED", "intent": "ENTRY"},
              {"ticker": "NTNX", "side": "BUY", "broker_order_id": "b2", "status": "FILLED", "intent": "ENTRY"}]
    broker_raw = [rh_order("b1", "GTLB", created="2026-10-15T13:30:00Z"),
                  rh_order("b2", "NTNX", created="2026-10-15T13:31:00Z"),
                  rh_order("x", "PD", created="2026-10-15T13:32:00Z"),
                  rh_order("y", "AI", state="cancelled", cum="0", created="2026-10-15T13:33:00Z")]
    res = compare(run, todays, broker_raw, D)
    got = {(r["ticker"], r["result"]) for r in res["rows"]}
    assert got == {("GTLB", "MATCH"), ("DT", "YOU SKIPPED"), ("NTNX", "EXECUTOR REFUSED"), ("PD", "OFF-PLAN")}
    assert res["counts"] == {"MATCH": 1, "YOU SKIPPED": 1, "EXECUTOR REFUSED": 1, "OFF-PLAN": 1}
