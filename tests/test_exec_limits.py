"""
Phase 2 gate (EXECUTOR_DESIGN.md §5): every execution limit is tested, and every
unknown fails closed. Pure functions only — no Supabase, no broker.
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import exec_limits as L  # noqa: E402
from exec_limits import ExecState, Mandate, check_submission, consecutive_stop_outs, effective_limits  # noqa: E402
from exec_state import build_exec_state  # noqa: E402
from trade_guard import GuardContext  # noqa: E402

MON = date(2026, 10, 12)
NOW = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)            # 10:00 ET
CLOSE = datetime(2026, 10, 12, 20, 0, tzinfo=timezone.utc)          # 16:00 ET


def gctx(**kw):
    base = dict(today=MON, equity=5000.0, buying_power=5000.0, positions={}, bought_today=set(),
                sold_today=set(), open_orders=[], managed={}, universe={"NVDA", "MU", "AAPL"})
    return GuardContext(**{**base, **kw})


def entry(ticker="NVDA", qty=5, limit=100.0, stop=96.0, oid=1, **kw):
    risk = qty * (limit - stop) if stop is not None else None
    return {"id": oid, "ticker": ticker, "side": "BUY", "intent": "ENTRY", "order_type": "LIMIT", "qty": qty,
            "limit_price": limit, "stop_price": stop, "report_date": MON, "risk_dollars": risk, **kw}


def exit_(ticker="NVDA", qty=5, intent="EXIT_TIME", oid=9, **kw):
    return {"id": oid, "ticker": ticker, "side": "SELL", "intent": intent, "order_type": "MARKET", "qty": qty, **kw}


def mandate(**kw):
    base = dict(mandate_date=MON, max_entries=2, max_total_risk_usd=50.0, allowed_tickers=frozenset({"NVDA", "MU"}),
                expires_at=CLOSE)
    return Mandate(**{**base, **kw})


def state(**kw):
    return ExecState(**{**dict(today=MON), **kw})


HELD = {"NVDA": {"qty": 5, "avg_cost": 100.0, "last_price": 101.0}}


def check(order, g=None, s=None, m="default", now=NOW, kind="SUBMIT", quote=None):
    return check_submission(order, g or gctx(), state() if s is None else s,
                            mandate() if m == "default" else m, now, quote, kind)


# ── The happy paths ────────────────────────────────────────────────────────────

def test_clean_entry_and_exit_pass():
    assert check(entry()) == []
    assert check(exit_(), g=gctx(positions=HELD, managed={"NVDA": {"opened_date": date(2026, 10, 9)}})) == []


# ── Fail closed ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("s", [None, ExecState(today=MON, complete=False), ExecState(today=date(2026, 10, 9))])
def test_unknown_execution_state_refuses_everything(s):
    assert "EXEC_STATE_UNKNOWN" in check_submission(entry(), gctx(), s, mandate(), NOW)
    assert "EXEC_STATE_UNKNOWN" in check_submission(exit_(), gctx(positions=HELD), s, mandate(), NOW)


def test_entries_need_a_live_mandate():
    assert "NO_MANDATE" in check(entry(), m=None)
    assert "MANDATE_REVOKED" in check(entry(), m=mandate(status="REVOKED"))
    assert "MANDATE_EXPIRED" in check(entry(), m=mandate(mandate_date=date(2026, 10, 9)))
    assert "MANDATE_EXPIRED" in check(entry(), now=CLOSE)


def test_exits_do_not_need_a_mandate():
    assert check(exit_(), g=gctx(positions=HELD), m=None) == []


def test_unknown_intent_and_kind():
    assert "UNKNOWN_INTENT" in check({**exit_(), "intent": "SCALP"}, g=gctx(positions=HELD))
    assert "UNKNOWN_KIND" in check(entry(), kind="BLAST")


def test_missing_risk_fails_the_budget():
    o = entry()
    del o["risk_dollars"]
    o["stop_price"] = None
    assert "MANDATE_RISK_BUDGET" in check(o)


# ── Layer 1 ceilings ──────────────────────────────────────────────────────────

def test_ceilings_are_the_accepted_values():
    assert (L.MAX_NEW_ENTRIES_DAY, L.MAX_ENTRIES_5_SESSIONS, L.MAX_OPEN_POSITIONS, L.MAX_ORDERS_DAY,
            L.MAX_RESUBMITS, L.MAX_CANCEL_REPLACE, L.MAX_STOP_OUTS_DAY, L.PDT_MAX_DAY_TRADES) == (2, 6, 5, 8, 1, 2, 2, 3)
    assert (L.MAX_POSITION_PCT, L.MAX_OPEN_RISK_PCT, L.RISK_PER_TRADE_PCT) == (30.0, 5.0, 2.5)
    assert (L.DAILY_LOSS_HALT_PCT, L.DRAWDOWN_HALT_PCT) == (3.0, 10.0)
    assert L.ALLOW_SAME_DAY_EXIT is True


# The numbers must keep fitting together as exec_limits.py says they do.
def test_how_the_risk_limits_fit_together():
    assert 2 * L.RISK_PER_TRADE_PCT == L.MAX_OPEN_RISK_PCT                  # two full-size trades fill open risk
    assert L.MAX_OPEN_RISK_PCT / L.MAX_OPEN_POSITIONS == 1.0                # five positions fit at 1% each
    assert L.RISK_PER_TRADE_PCT < L.DAILY_LOSS_HALT_PCT < 2 * L.RISK_PER_TRADE_PCT   # the second stop-out halts


def test_trade_guard_uses_the_same_numbers():
    import trade_guard
    assert trade_guard.MAX_NEW_ENTRIES_DAY is L.MAX_NEW_ENTRIES_DAY
    assert trade_guard.MAX_OPEN_POSITIONS is L.MAX_OPEN_POSITIONS


def test_entries_per_day():
    assert "MAX_ENTRIES_TODAY" not in check(entry(), s=state(entries_today={"MU"}))
    assert "MAX_ENTRIES_TODAY" in check(entry(), s=state(entries_today={"MU", "AAPL"}))


def test_entries_per_five_sessions():
    assert "MAX_ENTRIES_WEEK" not in check(entry(), s=state(entries_5_sessions=5))
    assert "MAX_ENTRIES_WEEK" in check(entry(), s=state(entries_5_sessions=6))


def test_open_positions_via_guard():
    four = {t: {"qty": 1, "avg_cost": 10} for t in ("MU", "AAPL", "MSFT", "AMD")}
    assert "MAX_POSITIONS" not in check(entry(), g=gctx(positions=four))
    five = {t: {"qty": 1, "avg_cost": 10} for t in ("MU", "AAPL", "MSFT", "AMD", "INTC")}
    assert "MAX_POSITIONS" in check(entry(), g=gctx(positions=five))


def test_orders_per_day_counts_every_intent():
    assert "MAX_ORDERS_TODAY" not in check(entry(), s=state(orders_submitted_today=7))
    assert "MAX_ORDERS_TODAY" in check(entry(), s=state(orders_submitted_today=8))
    assert "MAX_ORDERS_TODAY" in check(exit_(), g=gctx(positions=HELD), s=state(orders_submitted_today=8))


def test_one_retry_then_a_human():
    assert "ALREADY_SUBMITTED" in check(entry(oid=1), s=state(submits_by_order={1: 1}))
    assert "RESUBMIT_LIMIT" not in check(entry(oid=1), s=state(submits_by_order={1: 1}), kind="RESUBMIT")
    assert "RESUBMIT_LIMIT" in check(entry(oid=1), s=state(submits_by_order={1: 2}), kind="RESUBMIT")


def test_cancel_replace_per_order_per_day():
    assert "CANCEL_REPLACE_LIMIT" not in check(entry(oid=1), s=state(cancel_replace_today={1: 1}), kind="REPLACE")
    assert "CANCEL_REPLACE_LIMIT" in check(entry(oid=1), s=state(cancel_replace_today={1: 2}), kind="REPLACE")


def test_two_stop_outs_stop_entries_not_exits():
    s = state(exits_today=["EXIT_STOP", "PROTECT_STOP"])
    assert "STOP_OUT_HALT" in check(entry(), s=s)
    assert "STOP_OUT_HALT" not in check(entry(), s=state(exits_today=["EXIT_STOP", "EXIT_TARGET"]))
    assert check(exit_(), g=gctx(positions=HELD), s=s) == []
    assert consecutive_stop_outs(["EXIT_STOP", "EXIT_TARGET", "EXIT_STOP", "EXIT_STOP"]) == 2


def test_size_and_open_risk_and_halts_via_guard():
    assert "POSITION_TOO_LARGE" in check(entry(qty=20, stop=99.0, risk_dollars=20))
    assert "DAILY_LOSS_HALT" in check(entry(), g=gctx(day_pnl=-200.0))
    assert "DRAWDOWN_HALT" in check(entry(), g=gctx(equity_peak=6000.0))
    assert "HALTED" in check(entry(), g=gctx(halted=True))
    assert "HALTED" not in check(exit_(), g=gctx(positions=HELD, halted=True))     # exits still allowed


# ── Layer 2: the mandate can only tighten ─────────────────────────────────────

def test_mandate_cannot_loosen_ceilings():
    lim = effective_limits(mandate(max_entries=5, order_types=frozenset({"LIMIT", "MARKET"}),
                                   require_stop=False))
    assert lim.max_entries == 2 and lim.order_types == frozenset({"LIMIT"})
    assert lim.require_stop is True


def test_mandate_tickers_types_entries_and_budget():
    assert "NOT_IN_MANDATE" in check(entry("AAPL"))
    assert "ORDER_TYPE_NOT_ALLOWED" in check(entry(order_type="MARKET"))
    assert "STOP_REQUIRED" in check(entry(stop=None, risk_dollars=0))
    assert "MANDATE_MAX_ENTRIES" in check(entry(), m=mandate(max_entries=1), s=state(entries_today={"MU"}))
    assert "MANDATE_MAX_ENTRIES" in check(entry(), m=mandate(max_entries=0))
    assert "MANDATE_RISK_BUDGET" not in check(entry(), s=state(entry_risk_today=30.0))          # 30 + 20 = 50
    assert "MANDATE_RISK_BUDGET" in check(entry(), s=state(entry_risk_today=30.01))


# ── Same-day exits and PDT (allowed by the user 2026-11-02, re-buys are not) ──

def test_same_day_exit_passes_within_the_day_trade_count_with_or_without_a_mandate():
    g = gctx(positions=HELD, bought_today={"NVDA"})
    assert check(exit_(), g=g, s=state(day_trades_5_sessions=2)) == []
    assert check(exit_(), g=g, m=None, s=state(day_trades_5_sessions=2)) == []    # exits never need a mandate
    assert "PDT_LIMIT" in check(exit_(), g=g, s=state(day_trades_5_sessions=3))
    assert "PDT_LIMIT" not in check(exit_(), g=gctx(positions=HELD, bought_today={"NVDA"}, equity=30000.0),
                                    s=state(day_trades_5_sessions=3))


def test_same_day_exit_fails_closed_when_the_state_is_unknown():
    g = gctx(positions=HELD, bought_today={"NVDA"})
    assert {"PDT_LIMIT", "EXEC_STATE_UNKNOWN"} <= set(check(exit_(), g=g, s=state(complete=False)))


def test_same_day_rebuy_is_still_refused():
    assert "SAME_DAY_REBUY" in check(entry(), g=gctx(sold_today={"NVDA"}))


# ── State built from the ledger ───────────────────────────────────────────────

def test_build_exec_state_from_events_and_fills():
    sessions = [date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9), MON]
    orders = {1: {"intent": "ENTRY", "risk_dollars": 20}, 2: {"intent": "ENTRY", "risk_dollars": 15},
              3: {"intent": "EXIT_STOP"}, 4: {"intent": "PROTECT_STOP"}, 5: {"intent": "ENTRY", "risk_dollars": 9}}
    events = [
        {"order_id": 1, "kind": "SUBMIT", "intent": "ENTRY", "ticker": "NVDA", "trade_date": "2026-10-12"},
        {"order_id": 1, "kind": "RESUBMIT", "intent": "ENTRY", "ticker": "NVDA", "trade_date": "2026-10-12"},
        {"order_id": 1, "kind": "REPLACE", "intent": "ENTRY", "ticker": "NVDA", "trade_date": "2026-10-12"},
        {"order_id": 5, "kind": "SUBMIT", "intent": "ENTRY", "ticker": "AAPL", "trade_date": "2026-10-08"},
        {"order_id": 5, "kind": "SUBMIT", "intent": "ENTRY", "ticker": "AAPL", "trade_date": "2026-09-30"},   # outside
    ]
    fills = [
        {"order_id": 2, "ticker": "MU", "side": "BUY", "trade_date": "2026-10-12", "filled_at": "2026-10-12T14:00Z"},
        {"order_id": None, "ticker": "TSLA", "side": "BUY", "trade_date": "2026-10-09", "filled_at": "x"},  # untracked
        {"order_id": 3, "ticker": "X", "side": "SELL", "trade_date": "2026-10-12", "filled_at": "2026-10-12T15:00Z"},
        {"order_id": 4, "ticker": "Y", "side": "SELL", "trade_date": "2026-10-12", "filled_at": "2026-10-12T16:00Z"},
        {"order_id": 2, "ticker": "MU", "side": "SELL", "trade_date": "2026-10-12", "filled_at": "2026-10-12T17:00Z"},
    ]
    st = build_exec_state(MON, sessions, events, fills, orders)
    assert st.orders_submitted_today == 3                       # SUBMIT + RESUBMIT + REPLACE
    assert st.submits_by_order[1] == 2 and st.cancel_replace_today == {1: 1}
    assert st.entries_today == {"NVDA", "MU"}
    assert st.entries_5_sessions == 4                           # NVDA, MU today; AAPL Thu; untracked TSLA Fri
    assert st.entry_risk_today == 35.0
    assert st.exits_today == ["EXIT_STOP", "PROTECT_STOP", "UNKNOWN_EXIT"]
    assert st.day_trades_5_sessions == 1                        # MU bought and sold today
    assert st.complete


def test_failed_submit_does_not_use_an_entry_slot_or_budget():
    sessions = [date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9), MON]
    events = [{"order_id": 1, "kind": "SUBMIT", "intent": "ENTRY", "ticker": "NVDA", "trade_date": "2026-10-12", "ok": False}]
    st = build_exec_state(MON, sessions, events, [], {1: {"intent": "ENTRY", "risk_dollars": 20}})
    assert st.entries_today == set() and st.entry_risk_today == 0 and st.entries_5_sessions == 0
    assert st.orders_submitted_today == 1 and st.submits_by_order == {1: 1}     # still counts as an attempt
    assert check(entry(oid=1), s=st, kind="RESUBMIT") == []                     # so the one retry is allowed
    assert "RESUBMIT_LIMIT" in check(entry(oid=1), s=state(submits_by_order={1: 2}), kind="RESUBMIT")
