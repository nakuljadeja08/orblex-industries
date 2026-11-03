"""Tests for sync_fills.plan_sync — matching hand-placed broker orders to approved orders."""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sync_fills import plan_sync  # noqa: E402

MON, TUE = date(2026, 10, 12), date(2026, 10, 13)


def ours(id=1, status="APPROVED", **kw):
    return {"id": id, "status": status, "ticker": "NVDA", "side": "BUY", "qty": 4, "order_type": "LIMIT",
            "limit_price": 227.5, "stop_price": 223.0, "plan_date": "2026-10-12", "time_in_force": "DAY",
            "broker_order_id": None, **kw}


def broker(id="rh1", **kw):
    return {"id": id, "symbol": "NVDA", "side": "buy", "type": "limit", "price": "227.50", "quantity": "4",
            "state": "confirmed", "cumulative_quantity": "0", "created_at": "2026-10-12T13:05:00Z", **kw}


def test_links_matching_limit_order():
    p = plan_sync([ours()], [broker()], {}, MON)
    assert p["link"] == [(1, "rh1")] and p["status"] == {1: "PLACED"} and p["fills"] == []


def test_does_not_link_on_price_or_qty_mismatch():
    assert plan_sync([ours()], [broker(price="228.00")], {}, MON)["link"] == []
    assert plan_sync([ours()], [broker(quantity="5")], {}, MON)["link"] == []
    assert plan_sync([ours()], [broker(side="sell")], {}, MON)["link"] == []


def test_filled_records_fill_once():
    b = broker(state="filled", cumulative_quantity="4", average_price="227.41",
               last_transaction_at="2026-10-12T13:31:10Z")
    p = plan_sync([ours()], [b], {}, MON)
    assert p["status"][1] == "FILLED" and p["fills"][0]["qty"] == 4 and p["fills"][0]["trade_date"] == "2026-10-12"
    # Second run: already recorded → no duplicate fill.
    p2 = plan_sync([ours(status="FILLED", broker_order_id="rh1")], [b], {1: 4}, MON)
    assert p2["fills"] == []


def test_partial_then_cancelled_stays_partial():
    b = broker(state="cancelled", cumulative_quantity="2", average_price="227.5",
               last_transaction_at="2026-10-12T19:59:00Z")
    p = plan_sync([ours(status="PLACED", broker_order_id="rh1")], [b], {}, MON)
    assert p["status"][1] == "PARTIAL" and p["fills"][0]["qty"] == 2


def test_unfilled_cancel_and_expiry():
    p = plan_sync([ours(status="PLACED", broker_order_id="rh1")], [broker(state="cancelled")], {}, MON)
    assert p["status"][1] == "CANCELLED"
    assert plan_sync([ours()], [], {}, TUE)["status"] == {1: "EXPIRED"}     # approved, never placed
    assert plan_sync([ours()], [], {}, MON)["status"] == {}                  # still today: leave it


def test_stop_order_matches_on_stop_price():
    o = ours(side="SELL", order_type="STOP", time_in_force="GTC")
    b = broker(side="sell", type="market", trigger="stop", stop_price="223.00", price=None)
    assert plan_sync([o], [b], {}, MON)["link"] == [(1, "rh1")]


def test_one_broker_order_links_to_one_approval():
    p = plan_sync([ours(1), ours(2)], [broker("rh1")], {}, MON)
    assert p["link"] == [(1, "rh1")]


def test_untracked_fills_are_reported():
    b = broker("rhX", symbol="MU", state="filled", cumulative_quantity="1", last_transaction_at="2026-10-12T14:00:00Z")
    assert plan_sync([], [b], {}, MON)["untracked"][0]["symbol"] == "MU"


def test_price_near_miss_is_named_but_not_linked():
    # The Oct 14 case: approved at limit 16.20, placed at 17.07, filled next day.
    filled = broker(price="229.50", state="filled", cumulative_quantity="4", average_price="228.10",
                    last_transaction_at="2026-10-12T13:30:00Z")
    p = plan_sync([ours()], [filled], {}, TUE)
    assert p["link"] == [] and p["status"] == {1: "EXPIRED"}
    u = p["untracked"][0]
    assert (u["near_order"], u["plan_price"], u["placed_price"]) == (1, 227.5, 229.5)
    other = broker(id="rh2", symbol="MU", price="229.50", state="filled", cumulative_quantity="4")
    assert "near_order" not in plan_sync([ours()], [other], {}, TUE)["untracked"][0]


def test_finished_orders_are_not_reported_as_untracked():
    filled = broker(id="rh9", state="filled", cumulative_quantity="4")
    assert plan_sync([], [filled], {}, MON)["untracked"]                       # unknown → reported
    assert plan_sync([], [filled], {}, MON, known={"rh9"})["untracked"] == []  # linked to a FILLED plan order


def test_a_live_replacement_wins_over_its_cancelled_twin():
    ours = [{"id": 46, "ticker": "AI", "side": "BUY", "qty": 4, "order_type": "LIMIT", "limit_price": 11.4,
             "plan_date": "2026-10-22", "status": "APPROVED", "broker_order_id": None, "time_in_force": "DAY"}]
    def b(id, state, created):
        return {"id": id, "symbol": "AI", "side": "buy", "type": "limit", "state": state, "quantity": "4",
                "cumulative_quantity": "0", "price": "11.40", "created_at": created}
    broker = [b("gtc", "cancelled", "2026-10-22T14:24:03Z"), b("day", "confirmed", "2026-10-22T14:28:54Z")]
    plan = plan_sync(ours, broker, {}, date(2026, 10, 22))
    assert plan["link"] == [(46, "day")] and plan["status"][46] == "PLACED"
