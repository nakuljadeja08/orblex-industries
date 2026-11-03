"""
Tests for the pre-trade checks (trade_guard) and the plan builder (trade_plan).
Pure functions only — no Supabase, no broker.

    python -m pytest tests -q
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_guard import GuardContext, check_order  # noqa: E402
from trade_plan import build_plan, effective_level, size_entry  # noqa: E402

MON, FRI, THU = date(2026, 10, 12), date(2026, 10, 9), date(2026, 10, 8)


def ctx(**kw):
    base = dict(today=MON, equity=5000.0, buying_power=5000.0, positions={}, bought_today=set(),
                sold_today=set(), open_orders=[], managed={}, universe={"NVDA", "MU", "AAPL", "DDOG", "NOW", "MSFT"})
    return GuardContext(**{**base, **kw})


def entry(ticker="NVDA", qty=5, limit=100.0, stop=96.0, **kw):
    return {"ticker": ticker, "side": "BUY", "intent": "ENTRY", "order_type": "LIMIT", "qty": qty,
            "limit_price": limit, "stop_price": stop, "target_price": 108.0, "report_date": MON, **kw}


def sell(ticker="NVDA", qty=5, intent="EXIT_SIGNAL", **kw):
    return {"ticker": ticker, "side": "SELL", "intent": intent, "order_type": "MARKET", "qty": qty, **kw}


HELD = {"NVDA": {"qty": 5, "avg_cost": 100.0, "last_price": 101.0}}


# ── No day trading ──────────────────────────────────────────────────────────

def test_clean_entry_passes():
    assert check_order(entry(), ctx()) == []


def test_same_day_sell_is_allowed_up_to_the_day_trade_count():
    c = dict(positions=HELD, bought_today={"NVDA"})
    assert check_order(sell(), ctx(**c, day_trades_5_sessions=2)) == []
    assert "PDT_LIMIT" in check_order(sell(), ctx(**c, day_trades_5_sessions=3))
    assert "PDT_LIMIT" in check_order(sell(), ctx(**c))                        # count unknown: fail closed
    assert check_order(sell(), ctx(**c, equity=30000.0, buying_power=30000.0)) == []


def test_old_rule_returns_if_same_day_exits_are_switched_off(monkeypatch):
    import trade_guard
    monkeypatch.setattr(trade_guard, "ALLOW_SAME_DAY_EXIT", False)
    assert "SAME_DAY_SELL" in check_order(sell(), ctx(positions=HELD, bought_today={"NVDA"}, day_trades_5_sessions=0))


def test_cannot_rebuy_what_was_sold_today():
    assert "SAME_DAY_REBUY" in check_order(entry(), ctx(sold_today={"NVDA"}))


def test_protective_stop_on_fill_day_within_the_count():
    c = ctx(positions=HELD, managed={"NVDA": {"stop_price": 96.0, "opened_date": MON}}, day_trades_5_sessions=0)
    assert check_order(sell(intent="PROTECT_STOP", order_type="STOP"), c) == []
    c.day_trades_5_sessions = 3
    assert "PDT_LIMIT" in check_order(sell(intent="PROTECT_STOP", order_type="STOP"), c)
    c.managed["NVDA"]["opened_date"] = FRI                                     # held from before: no day trade
    assert check_order(sell(intent="PROTECT_STOP", order_type="STOP"), c) == []


def test_resting_stops_on_todays_other_fills_count_toward_the_limit():
    held = {**HELD, "MU": {"qty": 1, "avg_cost": 50.0, "last_price": 51.0}}
    rest = [{"ticker": "MU", "side": "SELL", "type": "STOP", "qty": 1}]
    c = ctx(positions=held, bought_today={"NVDA", "MU"}, open_orders=rest, day_trades_5_sessions=2)
    assert "PDT_LIMIT" in check_order(sell(intent="PROTECT_STOP", order_type="STOP"), c)
    c.day_trades_5_sessions = 1
    assert check_order(sell(intent="PROTECT_STOP", order_type="STOP"), c) == []


def test_no_sell_while_a_buy_is_working_and_vice_versa():
    working_buy = [{"ticker": "NVDA", "side": "BUY", "type": "LIMIT", "qty": 5}]
    assert "OPPOSITE_ORDER" in check_order(sell(), ctx(positions=HELD, open_orders=working_buy))
    working_sell = [{"ticker": "MU", "side": "SELL", "type": "STOP", "qty": 1}]
    assert "OPPOSITE_ORDER" in check_order(entry("MU"), ctx(open_orders=working_sell))


def test_exits_allowed_while_halted():
    assert check_order(sell(), ctx(positions=HELD, halted=True)) == []
    assert "HALTED" in check_order(entry("MU"), ctx(halted=True))


# ── Account limits ──────────────────────────────────────────────────────────

def test_sell_cannot_exceed_position():
    assert "SELL_EXCEEDS_POSITION" in check_order(sell(qty=6), ctx(positions=HELD))


def test_daily_loss_and_drawdown_halts():
    assert "DAILY_LOSS_HALT" in check_order(entry(), ctx(day_pnl=-160.0))
    assert "DRAWDOWN_HALT" in check_order(entry(), ctx(equity_peak=5600.0))


def test_position_and_risk_caps():
    assert "POSITION_TOO_LARGE" in check_order(entry(qty=16), ctx())          # $1600 > 30% of $5000
    assert "BUYING_POWER" in check_order(entry(qty=10), ctx(buying_power=900))
    big_risk = entry(qty=14, limit=100, stop=80)                               # $280 risk ≤ 5% … plus existing
    c = ctx(positions={"MU": {"qty": 1, "avg_cost": 1000, "last_price": 1000}})  # unmanaged → 10% = $100
    assert "OPEN_RISK_LIMIT" in check_order(big_risk, c)


def test_max_entries_and_positions():
    # Accepted limits (exec_limits.py): 2 new entries a day, 5 open positions.
    assert "MAX_ENTRIES_TODAY" not in check_order(entry(), ctx(bought_today={"AAPL"}))
    assert "MAX_ENTRIES_TODAY" in check_order(entry(), ctx(bought_today={"AAPL", "DDOG"}))
    four = {t: {"qty": 1, "avg_cost": 10} for t in ("AAPL", "DDOG", "NOW", "CRM")}
    assert "MAX_POSITIONS" not in check_order(entry(), ctx(positions=four))
    five = {t: {"qty": 1, "avg_cost": 10} for t in ("AAPL", "DDOG", "NOW", "CRM", "SNOW")}
    assert "MAX_POSITIONS" in check_order(entry(), ctx(positions=five))


def test_entry_sanity():
    assert "ENTRY_NOT_LIMIT" in check_order(entry(order_type="MARKET"), ctx())
    assert "BAD_LEVELS" in check_order(entry(stop=101.0), ctx())
    assert "QUOTE_BELOW_STOP" in check_order(entry(), ctx(), quote=95.5)
    assert "NOT_IN_UNIVERSE" in check_order(entry("TSLA"), ctx())
    assert "ALREADY_IN_TICKER" in check_order(entry(), ctx(positions=HELD))


def test_plan_validity_window():
    assert check_order(entry(report_date=FRI), ctx()) == []                    # previous session: still live
    assert "STALE_PLAN" in check_order(entry(report_date=THU), ctx())          # two sessions ago: expired


# ── Sizing and plan ─────────────────────────────────────────────────────────

def plan(t="NVDA", strength="MODERATE", entry_px=100.0, stop=96.0, rr=2.0):
    return {"run_id": "r", "ticker": t, "report_date": MON, "signal": "BUY", "signal_strength": strength,
            "setup_type": "PULLBACK", "entry_price": entry_px, "stop_price": stop, "target_price": 108.0,
            "horizon_days": 5, "risk_reward": rr}


def test_sizing_risks_the_budget_scaled_by_strength_and_level():
    # RISK_PER_TRADE_PCT = 2.5%. Stop $12 away keeps the risk budget (not the 30%
    # notional cap of 15 sh) the binding factor, so strength/level scaling shows.
    assert size_entry(plan(strength="STRONG", stop=88.0), 5000, "LOW", None) == (10, 120.0)   # $125 / $12 → 10
    assert size_entry(plan(strength="MODERATE", stop=88.0), 5000, "LOW", None)[0] == 7         # $93.75 / $12
    assert size_entry(plan(strength="STRONG", stop=88.0), 5000, "HIGH", None)[0] == 5          # half risk → $62.5
    assert size_entry(plan(strength="STRONG", stop=88.0), 5000, "CRITICAL", None)[0] == 0
    assert size_entry(plan(strength="STRONG", stop=88.0), 5000, "LOW", 10.0)[0] == 5           # risk cap 10% → $500 notional


def test_effective_level_only_escalates():
    assert effective_level("LOW", "HIGH") == "HIGH"
    assert effective_level("CRITICAL", None) == "CRITICAL"


def test_plan_ranks_and_blocks_excess_entries():
    plans = [plan("MU", "WEAK"), plan("NVDA", "STRONG"), plan("AAPL", "MODERATE", rr=3.0),
             plan("DDOG", "MODERATE", rr=1.6)]
    out = build_plan(ctx(), plans, {}, {}, {"MU": {"risk_score": 3}}, "LOW")
    order = [(o["ticker"], o["status"]) for o in out["entries"]]
    assert order[0] == ("NVDA", "PROPOSED") and order[1] == ("AAPL", "PROPOSED")
    assert order[2][1] == "BLOCKED" and order[3] == ("MU", "BLOCKED")   # 3rd and 4th exceed MAX_NEW_ENTRIES_DAY (2)


def test_plan_excludes_high_risk_score():
    out = build_plan(ctx(), [plan()], {}, {}, {"NVDA": {"risk_score": 8}}, "LOW")
    assert out["entries"][0]["guard_reasons"] == ["RISK_SCORE_EXCLUDED"]


def test_plan_exits():
    managed = {"NVDA": {"stop_price": 96.0, "target_price": 108.0, "horizon_days": 5, "opened_date": FRI}}
    c = ctx(positions=HELD, managed=managed)
    assert build_plan(c, [], {}, {"NVDA": 101.0}, {}, "LOW")["exits"][0]["intent"] == "PROTECT_STOP"
    c = ctx(positions=HELD, managed=managed)
    assert build_plan(c, [], {"NVDA": "SELL"}, {"NVDA": 101.0}, {}, "LOW")["exits"][0]["intent"] == "EXIT_SIGNAL"
    c = ctx(positions=HELD, managed={"NVDA": {**managed["NVDA"], "opened_date": date(2026, 10, 1)}})
    assert build_plan(c, [], {}, {"NVDA": 101.0}, {}, "LOW")["exits"][0]["intent"] == "EXIT_TIME"
    c = ctx(positions=HELD, managed=managed)
    assert build_plan(c, [], {}, {"NVDA": 95.0}, {}, "LOW")["exits"][0]["intent"] == "EXIT_STOP"


def test_exit_of_todays_buy_is_proposed_until_the_day_trade_limit():
    managed = {"NVDA": {"stop_price": 96.0, "target_price": 108.0, "horizon_days": 5, "opened_date": MON}}
    c = ctx(positions=HELD, managed=managed, bought_today={"NVDA"}, day_trades_5_sessions=0)
    assert build_plan(c, [], {"NVDA": "SELL"}, {"NVDA": 101.0}, {}, "LOW")["exits"][0]["status"] == "PROPOSED"
    c = ctx(positions=HELD, managed=managed, bought_today={"NVDA"}, day_trades_5_sessions=3)
    ex = build_plan(c, [], {"NVDA": "SELL"}, {"NVDA": 101.0}, {}, "LOW")["exits"][0]
    assert ex["status"] == "BLOCKED" and "PDT_LIMIT" in ex["guard_reasons"]


def test_live_executor_owns_stops_time_exits_and_stop_exits():
    managed = {"NVDA": {"stop_price": 96.0, "target_price": 108.0, "horizon_days": 5, "opened_date": FRI}}
    plan_for = lambda signals, close: build_plan(ctx(positions=HELD, managed=managed), [], signals, {"NVDA": close},
                                                 {}, "LOW", executor_live=True)["exits"]
    assert plan_for({}, 101.0) == []                                            # PROTECT_STOP: the executor's OCO
    assert plan_for({}, 95.0) == []                                             # EXIT_STOP: its next pass
    assert plan_for({"NVDA": "SELL"}, 101.0)[0]["intent"] == "EXIT_SIGNAL"      # still your approval
    assert plan_for({}, 109.0)[0]["intent"] == "EXIT_TARGET"


def test_unmanaged_position_left_alone():
    out = build_plan(ctx(positions={"MU": {"qty": 1, "avg_cost": 1000}}), [], {"MU": "SELL"}, {}, {}, "LOW")
    assert out["exits"] == [] and out["unmanaged"] == ["MU"]
