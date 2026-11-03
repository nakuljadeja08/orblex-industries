import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobs import build_review, unprotected_positions  # noqa: E402

D = date(2026, 10, 15)
POS = {"data": {"positions": [{"symbol": "PD", "quantity": "2", "average_buy_price": "16.66"},
                              {"symbol": "SCHG", "quantity": "3", "average_buy_price": "35.41"},
                              {"symbol": "GTLB", "quantity": "1", "average_buy_price": "53.0"},
                              {"symbol": "NTNX", "quantity": "0"}]}}


def stop(sym, qty, state="confirmed", **kw):
    return {"symbol": sym, "side": "sell", "type": "market", "trigger": "stop", "state": state, "quantity": qty, **kw}


def test_unprotected_positions():
    orders = {"data": {"orders": [stop("GTLB", "1"), stop("PD", "1"), stop("SCHG", "3", state="cancelled")]}}
    out = unprotected_positions(POS, orders)
    assert out == [{"ticker": "PD", "qty": 2.0, "covered": 1.0},        # GTLB covered
                   {"ticker": "SCHG", "qty": 3.0, "covered": 0.0}]      # a fill today needs its stop too


def test_build_review_flags_the_test_run_issues():
    et = lambda h, m: datetime(2026, 10, 15, h, m, tzinfo=ZoneInfo("America/New_York")).isoformat()
    orders = [
        {"id": 20, "ticker": "SCHG", "intent": "ENTRY", "status": "FILLED", "limit_price": 35.75, "stop_price": 35.2,
         "created_at": et(9, 32), "approved_at": et(9, 36), "broker_order_id": "b1", "guard_reasons": []},
        {"id": 19, "ticker": "DT", "intent": "ENTRY", "status": "BLOCKED", "created_at": et(9, 32),
         "approved_at": None, "broker_order_id": None, "guard_reasons": ["BAD_PRICE_DATA"]},
    ]
    broker = [{"id": "b1", "created_at": et(9, 35), "last_transaction_at": et(9, 35), "cumulative_quantity": "3",
               "average_price": "35.415"}]
    rev = build_review(D, orders, [], broker, {"data": {"total_value": "497.75", "cash": "236.40"}}, POS,
                       datetime.fromisoformat(et(9, 40)), "MATCH 1", [{"ticker": "PD", "qty": 2.0, "covered": 0}])
    assert rev["plan"]["counts"] == {"FILLED": 1, "BLOCKED": 1}
    assert rev["orders"][0]["placed_before_approval"] is True and rev["orders"][0]["fill_price"] == 35.415
    assert rev["latency_median_min"]["plan_to_approve"] == 4.0
    assert "plan built before the pipeline finished" in rev["issues"]
    assert "an order was placed before its approval" in rev["issues"]
    assert any("without a stop" in i for i in rev["issues"]) and any("BAD_PRICE_DATA" in i for i in rev["issues"])
    assert rev["account"]["total_value"] == 497.75 and len(rev["account"]["positions"]) == 3
