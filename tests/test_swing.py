"""
Tests for swing mode: the trade simulator (swing_backtest) and the post-model
rules (swing_policy). Synthetic bars only — no Supabase, no network.

    python -m pytest tests -q
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest_engine import COST_BPS_PER_SIDE  # noqa: E402
from swing_backtest import (  # noqa: E402
    CANCELLED_GAP, NO_FILL, OPEN, PENDING, SIGNAL_EXIT, SKIPPED, STOP, TARGET, TIME,
    simulate_ticker, simulate_trade, summarize,
)
from swing_policy import enforce_swing_rules, sessions_missing  # noqa: E402

D0 = date(2026, 9, 1)   # a Tuesday; bars below are consecutive weekdays from here


def bars(*ohlc):
    """Consecutive weekday bars starting D0 from (open, high, low, close) tuples."""
    days, d = [], D0
    while len(days) < len(ohlc):
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return pd.DataFrame([{"date": dt, "open": o, "high": h, "low": l, "close": c}
                         for dt, (o, h, l, c) in zip(days, ohlc)])


def plan(**kw):
    return {"run_id": "r1", "ticker": "TST", "report_date": D0, "entry_price": 100.0,
            "stop_price": 96.0, "target_price": 108.0, "horizon_days": 5, **kw}


FLAT = (101, 102, 100.5, 101)


# ── Entry ───────────────────────────────────────────────────────────────────

def test_fills_at_limit_when_low_touches():
    r = simulate_trade(plan(), bars((102, 103, 99.5, 101), FLAT, FLAT, FLAT, FLAT, FLAT))
    assert r["fill_price"] == 100.0 and r["fill_date"] == D0


def test_gap_below_limit_fills_at_open():
    r = simulate_trade(plan(), bars((98, 99, 97.5, 98.5), FLAT, FLAT, FLAT, FLAT, FLAT))
    assert r["fill_price"] == 98.0


def test_no_fill_after_window_expires():
    assert simulate_trade(plan(), bars(FLAT, FLAT, FLAT))["outcome"] == NO_FILL


def test_short_window_is_still_pending():
    assert simulate_trade(plan(), bars(FLAT))["outcome"] == PENDING


def test_open_below_stop_cancels_entry():
    assert simulate_trade(plan(), bars((95, 97, 94, 96)))["outcome"] == CANCELLED_GAP


# ── No day trading ──────────────────────────────────────────────────────────

def test_stop_on_fill_day_is_not_acted_on_until_next_session():
    # Fill at 100 and crash to 95 the same day: can't sell. Next day opens 97,
    # recovers, then hits the target.
    r = simulate_trade(plan(), bars((101, 101, 95, 95.5), (97, 100, 96.5, 99), (100, 109, 99, 108)))
    assert r["outcome"] == TARGET
    assert r["exit_date"] > r["fill_date"]


def test_target_on_fill_day_is_not_taken():
    r = simulate_trade(plan(), bars((100, 110, 99, 109), (109, 109.5, 104, 105), FLAT, FLAT, FLAT, FLAT))
    # Day 1 opens at 109 ≥ target → exits at that open, never on the fill day.
    assert r["outcome"] == TARGET and r["exit_price"] == 109 and r["hold_sessions"] == 1


# ── Exits ───────────────────────────────────────────────────────────────────

def test_stop_touch_exits_at_stop():
    r = simulate_trade(plan(), bars((100, 101, 99.5, 100), (99, 99.5, 95.5, 96)))
    assert r["outcome"] == STOP and r["exit_price"] == 96.0
    assert r["r_multiple"] == pytest.approx(-1.025)   # −1R plus 10 bps of costs on $4 risk


def test_gap_through_stop_exits_at_open_beyond_1r():
    r = simulate_trade(plan(), bars((100, 101, 99.5, 100), (93, 94, 92, 93)))
    assert r["outcome"] == STOP and r["exit_price"] == 93
    assert r["r_multiple"] < -1.5


def test_gap_fill_near_stop_measures_r_against_planned_risk():
    # Opens at 96.05 — below the 100 limit, just above the 96 stop — so the limit
    # fills at the open with 5 cents to the stop. R must use the planned $4 risk
    # (what the account sizes from), not the 5 cents, which would read ≈ −2.9R.
    r = simulate_trade(plan(), bars((96.05, 96.5, 96.02, 96.3), (96.2, 96.3, 95.9, 96)))
    assert r["fill_price"] == 96.05 and r["outcome"] == STOP and r["exit_price"] == 96.0
    cost = 2 * COST_BPS_PER_SIDE / 1e4
    assert r["r_multiple"] == pytest.approx((96 - 96.05 - cost * 96.05) / 4, abs=1e-3)
    assert r["mfe_r"] == pytest.approx(0.25 / 4, abs=0.01)
    assert r["mae_r"] == pytest.approx(-0.15 / 4, abs=0.01)
    assert r["return_pct"] == pytest.approx((96 / 96.05 - 1 - cost) * 100, abs=1e-3)


def test_bar_touching_both_counts_as_stop():
    r = simulate_trade(plan(), bars((100, 101, 99.5, 100), (100, 109, 95, 104)))
    assert r["outcome"] == STOP


def test_target_hit_is_2r_net_of_costs():
    r = simulate_trade(plan(), bars((100, 101, 99.5, 100), (101, 108.5, 100.5, 108)))
    assert r["outcome"] == TARGET and r["exit_price"] == 108.0
    assert 1.95 < r["r_multiple"] < 2.0     # 2R minus 10 bps of round-trip cost


def test_time_exit_at_horizon_close():
    r = simulate_trade(plan(horizon_days=3), bars((100, 101, 99.5, 100), FLAT, FLAT, (101, 102, 100, 102.5)))
    assert r["outcome"] == TIME and r["exit_price"] == 102.5 and r["hold_sessions"] == 3


def test_open_trade_when_bars_run_out():
    assert simulate_trade(plan(), bars((100, 101, 99.5, 100), FLAT))["outcome"] == OPEN


def test_later_sell_signal_exits_at_open():
    b = bars((100, 101, 99.5, 100), FLAT, (101.5, 103, 101, 102), FLAT)
    r = simulate_trade(plan(), b, sell_dates=[b["date"][2]])
    assert r["outcome"] == SIGNAL_EXIT and r["exit_price"] == 101.5


# ── One trade at a time ─────────────────────────────────────────────────────

def test_second_plan_while_in_trade_is_skipped():
    b = bars((100, 101, 99.5, 100), FLAT, FLAT, (101, 108.5, 100.5, 108), FLAT, FLAT, FLAT)
    res = simulate_ticker([plan(), plan(run_id="r2", report_date=b["date"][1])], b, [])
    assert [r["outcome"] for r in res] == [TARGET, SKIPPED]


def test_no_rebuy_on_exit_session():
    # Trade 1 stops out on day 1. Plan 2 dated day 2 could fill on day 2 (low 99)
    # — allowed, since day 2 is after the exit session.
    b = bars((100, 101, 99.5, 100), (99, 99.5, 95.5, 96), (100, 101, 99, 100), FLAT, FLAT, FLAT, FLAT, FLAT)
    res = simulate_ticker([plan(), plan(run_id="r2", report_date=b["date"][1])], b, [])
    # Plan 2 is dated the exit session itself, so it is skipped (no same-day re-buy).
    assert [r["outcome"] for r in res] == [STOP, SKIPPED]
    res = simulate_ticker([plan(), plan(run_id="r3", report_date=b["date"][2])], b, [])
    assert res[1]["fill_date"] == b["date"][2]


def test_summary_expectancy():
    trades = [{"outcome": TARGET, "fill_date": D0, "r_multiple": 2.0, "return_pct": 8.0,
               "hold_sessions": 3, "setup_type": "PULLBACK", "signal_strength": "MODERATE", "ticker": "A"},
              {"outcome": STOP, "fill_date": D0, "r_multiple": -1.0, "return_pct": -4.0,
               "hold_sessions": 2, "setup_type": "PULLBACK", "signal_strength": "MODERATE", "ticker": "A"},
              {"outcome": NO_FILL, "fill_date": None}]
    s = summarize(trades)["overall"]
    assert s["n"] == 2 and s["win_rate"] == 0.5 and s["avg_r"] == 0.5 and s["profit_factor"] == 2.0
    assert summarize(trades)["fill_rate"] == pytest.approx(0.667, abs=0.001)


# ── swing_policy ────────────────────────────────────────────────────────────

IND = {"latest_close": 100.0, "atr_14": 2.0, "latest_date": "2026-10-09"}
MON = date(2026, 10, 12)


def enforce(a, earnings=None, ind=IND):
    a = {"signal": "BUY", "signal_strength": "STRONG", "entry_price": 100, "stop_price": 97,
         "target_price": 106, "horizon_days": 5, **a}
    return enforce_swing_rules(a, ind, earnings or {}, MON)


def test_valid_plan_passes_untouched():
    a = enforce({})
    assert a["signal"] == "BUY" and a["swing_flags"] == [] and a["risk_reward"] == 2.0


def test_low_reward_risk_downgrades():
    assert enforce({"target_price": 102})["signal"] == "HOLD"


def test_earnings_inside_window_blocks_or_clamps():
    assert enforce({}, {"next_earnings_date": "2026-10-13"})["signal"] == "HOLD"
    a = enforce({"horizon_days": 8}, {"next_earnings_date": "2026-10-19"})
    assert a["signal"] == "BUY" and a["horizon_days"] == 4


def test_stale_bars_block_entry():
    a = enforce({}, ind={**IND, "latest_date": "2026-10-05"})
    assert a["signal"] == "HOLD" and "STALE_PRICE_DATA" in a["swing_flags"]


def test_pullback_entry_allowed_two_atr_below():
    assert enforce({"entry_price": 96.5, "stop_price": 93.5, "target_price": 103})["entry_price"] == 96.5


def test_sessions_missing_uses_nyse_calendar():
    assert sessions_missing("2026-10-09", date(2026, 10, 12)) == 0   # Fri → Mon
    assert sessions_missing("2026-11-25", date(2026, 11, 27)) == 0   # Thanksgiving closed
