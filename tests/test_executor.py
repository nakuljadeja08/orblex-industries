"""
Executor + SimBroker, deterministic (no network): crash/restart idempotency, broker
errors and retries, reconcile halts, the STOP file, halts, mandates, and the
protective-stop / time-exit lifecycle.
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from broker import BrokerError, OrderTicket, SimBroker  # noqa: E402
from exec_limits import Mandate  # noqa: E402
from executor import Executor, MemoryLedger, SimulatedCrash  # noqa: E402

S = [date(2026, 10, d) for d in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16)]
D0, D1, D2 = S[4], S[5], S[6]


def mandate(d, tickers=("NVDA", "MU"), entries=2, risk=100.0):
    return Mandate(mandate_date=d, max_entries=entries, max_total_risk_usd=risk, allowed_tickers=frozenset(tickers),
                   expires_at=datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc).replace(hour=20))


def setup(tmp_path, tickers=("NVDA",), d=D0, horizon=3):
    broker = SimBroker(cash=5000.0)
    ledger = MemoryLedger()
    ledger.mandates[d] = mandate(d)
    for k, t in enumerate(tickers):
        ledger.add_order({"plan_date": d.isoformat(), "report_date": d, "ticker": t, "side": "BUY", "intent": "ENTRY",
                          "order_type": "LIMIT", "qty": 5, "limit_price": 100.0, "stop_price": 96.0,
                          "target_price": 108.0, "horizon_days": horizon, "risk_dollars": 20.0, "rank": k})
    ex = Executor(broker, ledger, S, stop_file=str(tmp_path / "STOP"))
    return broker, ledger, ex


BAR = {"open": 100.5, "high": 101.0, "low": 99.5, "close": 100.8}


def test_entry_submitted_once_even_with_two_passes(tmp_path):
    broker, ledger, ex = setup(tmp_path)
    ex.pre_open(D0)
    ex.pre_open(D0)
    assert len(broker.orders()) == 1 and broker.orders()[0].ticket.client_order_id == "wi-1"


def test_crash_after_broker_accepts_is_recovered_without_resending(tmp_path):
    broker, ledger, ex = setup(tmp_path)
    ex.crash_at = "after_submit"
    with pytest.raises(SimulatedCrash):
        ex.pre_open(D0)
    assert len(broker.orders()) == 1 and ledger.orders[1]["status"] == "APPROVED"     # broker has it, ledger doesn't
    Executor(broker, ledger, S, stop_file=ex.stop_file).pre_open(D0)                  # restart
    assert len(broker.orders()) == 1 and broker.submit_calls == 1
    assert ledger.orders[1]["status"] == "PLACED" and ledger.orders[1]["broker_order_id"] == "sim-1"
    assert [e["detail"] for e in ledger.events] == ["recovered after restart"]


def test_crash_before_submit_sends_exactly_once_after_restart(tmp_path):
    broker, ledger, ex = setup(tmp_path)
    ex.crash_at = "before_submit"
    with pytest.raises(SimulatedCrash):
        ex.pre_open(D0)
    assert broker.orders() == []
    Executor(broker, ledger, S, stop_file=ex.stop_file).pre_open(D0)
    assert len(broker.orders()) == 1


def test_broker_error_then_one_retry_then_a_human(tmp_path):
    broker, ledger, ex = setup(tmp_path)
    broker.fail_next = 1
    ex.pre_open(D0)
    assert broker.orders() == [] and [(e["kind"], e["ok"]) for e in ledger.events] == [("SUBMIT", False)]
    ex.pre_open(D0)                                                                    # next loop pass
    assert [(e["kind"], e["ok"]) for e in ledger.events] == [("SUBMIT", False), ("RESUBMIT", True)]

    broker2, ledger2, ex2 = setup(tmp_path)
    broker2.fail_next = 5
    ex2.pre_open(D0)
    ex2.pre_open(D0)
    ex2.pre_open(D0)
    assert [(e["kind"], e["ok"]) for e in ledger2.events] == [("SUBMIT", False), ("RESUBMIT", False)]
    assert "RESUBMIT_LIMIT" in ledger2.refusals[-1][4]


def test_three_broker_errors_in_a_row_stop_submissions_for_the_day(tmp_path):
    broker, ledger, ex = setup(tmp_path, tickers=("NVDA", "MU", "AAPL"))
    ledger.mandates[D0] = mandate(D0, tickers=("NVDA", "MU", "AAPL"), risk=100)
    broker.fail_next = 3
    ex.pre_open(D0)
    assert [(e["ticker"], e["ok"]) for e in ledger.events] == [("NVDA", False), ("MU", False), ("AAPL", False)]
    assert any("3 broker errors in a row" in a for a in ledger.alerts)
    ex.pre_open(D0)                                               # same day, broker healthy again: still stopped
    assert broker.orders() == [] and "BROKER_ERRORS_TODAY" in ledger.refusals[-1][4]


def test_reconcile_mismatch_halts_everything(tmp_path):
    broker, ledger, ex = setup(tmp_path)
    broker.positions["TSLA"] = {"qty": 3, "avg_cost": 200.0}                           # bought outside the executor
    out = ex.pre_open(D0)
    assert out["status"] == "HALTED" and broker.orders() == [] and ledger.halted
    assert "TSLA" in ledger.halt_reason


def test_untracked_broker_order_halts(tmp_path):
    broker, ledger, ex = setup(tmp_path)
    broker.submit(OrderTicket("manual-1", "NVDA", "BUY", "LIMIT", 1, limit_price=90.0))
    assert ex.pre_open(D0)["status"] == "HALTED" and len(broker.orders()) == 1


def test_stop_file_does_nothing(tmp_path):
    broker, ledger, ex = setup(tmp_path)
    (tmp_path / "STOP").write_text("stop")
    assert ex.pre_open(D0)["status"] == "STOP_FILE" and broker.orders() == []


def test_halt_and_missing_mandate_refuse_entries(tmp_path):
    broker, ledger, ex = setup(tmp_path)
    ledger.halted = True
    ex.pre_open(D0)
    assert broker.orders() == [] and "HALTED" in ledger.refusals[-1][4]
    broker, ledger, ex = setup(tmp_path)
    ledger.mandates.clear()
    ex.pre_open(D0)
    assert broker.orders() == [] and "NO_MANDATE" in ledger.refusals[-1][4]


def test_protective_oco_next_session_then_time_exit(tmp_path):
    broker, ledger, ex = setup(tmp_path, horizon=2)
    ex.pre_open(D0)
    broker.run_session(D0, {"NVDA": BAR})                         # fills at the 100.0 limit
    ex.post_close(D0)
    assert broker.positions["NVDA"]["qty"] == 5
    assert not [o for o in broker.orders() if o.ticket.side == "SELL"]   # the morning pass ran before the fill

    ex.pre_open(D1)                                               # next pass: protective OCO
    oco = [o for o in broker.orders() if o.ticket.order_type == "OCO"]
    assert len(oco) == 1 and oco[0].ticket.stop_price == 96.0 and oco[0].ticket.take_profit_price == 108.0
    ex.pre_open(D1)                                               # second pass: no second OCO
    assert len([o for o in broker.orders() if o.ticket.order_type == "OCO"]) == 1
    broker.run_session(D1, {"NVDA": BAR})
    ex.post_close(D1)

    ex.pre_open(D2)                                               # 1 session completed since the fill: hold
    assert oco[0].state == "WORKING" and not [o for o in broker.orders() if o.ticket.order_type == "MARKET"]
    broker.run_session(D2, {"NVDA": BAR})
    ex.post_close(D2)

    D3 = S[7]
    ex.pre_open(D3)                                               # horizon 2 completed: cancel OCO, sell at the open
    assert oco[0].state == "CANCELLED"
    mkt = [o for o in broker.orders() if o.ticket.order_type == "MARKET"]
    assert len(mkt) == 1 and mkt[0].ticket.qty == 5
    broker.run_session(D3, {"NVDA": BAR})
    ex.post_close(D3)
    assert broker.account().positions == {} and ex.reconcile() == []


def test_later_pass_protects_a_fill_the_same_day_then_no_rebuy_after_the_stop(tmp_path):
    broker, ledger, ex = setup(tmp_path, horizon=9)
    ex.pre_open(D0)
    broker.run_session(D0, {"NVDA": BAR})                         # filled during the session
    ex.post_close(D0)
    ex.pre_open(D0)                                               # a later pass, same day
    oco = [o for o in broker.orders() if o.ticket.order_type == "OCO"]
    assert len(oco) == 1 and oco[0].ticket.stop_price == 96.0

    broker.run_session(D0, {"NVDA": {"open": 99, "high": 99, "low": 95, "close": 95.5}})    # stopped out today
    ex.post_close(D0)
    assert ex._exec_state(D0).day_trades_5_sessions == 1

    ledger.add_order({"plan_date": D0.isoformat(), "report_date": D0, "ticker": "NVDA", "side": "BUY", "intent": "ENTRY",
                      "order_type": "LIMIT", "qty": 5, "limit_price": 96.0, "stop_price": 92.0, "target_price": 104.0,
                      "horizon_days": 5, "risk_dollars": 20.0, "rank": 0})
    ex.pre_open(D0)
    assert "SAME_DAY_REBUY" in ledger.refusals[-1][4]
    assert len([o for o in broker.orders() if o.ticket.side == "BUY"]) == 1


def test_fourth_day_trade_leaves_the_fill_unprotected_and_refused(tmp_path):
    broker, ledger, ex = setup(tmp_path, horizon=9)
    for i, t in enumerate(("AAA", "BBB", "CCC")):
        ledger.fills += [{"order_id": None, "ticker": t, "side": s, "qty": 1, "price": 10.0, "trade_date": S[2 + i].isoformat(),
                          "seq": 100 + 2 * i + (s == "SELL"), "filled_at": f"{S[2 + i]}T{s}"} for s in ("BUY", "SELL")]
    ex.pre_open(D0)
    broker.run_session(D0, {"NVDA": BAR})
    ex.post_close(D0)
    ex.pre_open(D0)
    assert not [o for o in broker.orders() if o.ticket.order_type == "OCO"]
    assert "PDT_LIMIT" in ledger.refusals[-1][4]


def test_oco_stop_out_counts_and_target_does_not(tmp_path):
    broker, ledger, ex = setup(tmp_path, horizon=9)
    ex.pre_open(D0); broker.run_session(D0, {"NVDA": BAR}); ex.post_close(D0)
    ex.pre_open(D1)
    broker.run_session(D1, {"NVDA": {"open": 99, "high": 109, "low": 95, "close": 97}})   # both touched → stop
    ex.post_close(D1)
    sell = [f for f in ledger.fills if f["side"] == "SELL"][0]
    assert sell["leg"] == "STOP" and sell["price"] == 96.0
    assert ledger.orders[sell["order_id"]]["fill_intent"] == "PROTECT_STOP"


def test_simbroker_rejects_oversell_and_is_idempotent():
    b = SimBroker(cash=1000.0)
    with pytest.raises(BrokerError):
        b.submit(OrderTicket("wi-1", "X", "SELL", "MARKET", 1))
    t = OrderTicket("wi-2", "X", "BUY", "LIMIT", 1, limit_price=10.0)
    assert b.submit(t) is b.submit(t) and len(b.orders()) == 1


def test_broker_error_block_survives_a_restart(tmp_path):
    broker, ledger, ex = setup(tmp_path, tickers=("NVDA", "MU", "AAPL"))
    ledger.mandates[D0] = mandate(D0, tickers=("NVDA", "MU", "AAPL"), risk=100)
    broker.fail_next = 3
    ex.pre_open(D0)
    Executor(broker, ledger, S, stop_file=ex.stop_file).pre_open(D0)      # restarted process, same day
    assert broker.orders() == []


def test_approved_plan_exit_cancels_the_stop_and_sells(tmp_path):
    broker, ledger, ex = setup(tmp_path, horizon=9)
    ex.pre_open(D0); broker.run_session(D0, {"NVDA": BAR}); ex.post_close(D0)
    ex.pre_open(D1); broker.run_session(D1, {"NVDA": BAR}); ex.post_close(D1)          # OCO resting
    ledger.add_order({"plan_date": D2.isoformat(), "report_date": D2, "ticker": "NVDA", "side": "SELL",
                      "intent": "EXIT_SIGNAL", "order_type": "MARKET", "qty": 5})
    ex.pre_open(D2)
    ex.pre_open(D2)                                                                     # second pass: nothing new
    oco = [o for o in broker.orders() if o.ticket.order_type == "OCO"]
    mkt = [o for o in broker.orders() if o.ticket.order_type == "MARKET"]
    assert oco[0].state == "CANCELLED" and len(mkt) == 1 and mkt[0].ticket.qty == 5
    assert [e["kind"] for e in ledger.events if e["trade_date"] == D2.isoformat()] == ["CANCEL", "SUBMIT"]


def test_position_already_through_its_stop_exits_at_the_open(tmp_path):
    broker, ledger, ex = setup(tmp_path, horizon=9)
    ex.pre_open(D0); broker.run_session(D0, {"NVDA": BAR}); ex.post_close(D0)          # filled at 100, stop 96
    broker.last_close["NVDA"] = 95.0                                                   # closed under the stop
    ex.pre_open(D1)
    sells = [o for o in broker.orders() if o.ticket.side == "SELL"]
    assert len(sells) == 1 and sells[0].ticket.order_type == "MARKET"                  # no OCO above the market
    assert ledger.orders[_oid_of(sells[0])]["intent"] == "EXIT_STOP"


def _oid_of(bo):
    return int(bo.ticket.client_order_id[3:])
