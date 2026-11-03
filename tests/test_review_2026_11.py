"""
Decisions from the 2026-11-02 architecture review: the v1/v2 gate counts closed trades, the
allocator, report and watchdog are weekly on the swing book, and the swing email reads no old
plan. Fill-day stops are in test_trade_guard / test_executor / test_exec_limits.
"""

import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_all import WEEKLY_STAGES, default_stages  # noqa: E402
from swing_backtest import STOP, simulate_trade  # noqa: E402


# ── The gate ───────────────────────────────────────────────────────────────────

@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", os.environ.get("SUPABASE_URL", "http://x"))
    monkeypatch.setenv("SUPABASE_KEY", os.environ.get("SUPABASE_KEY", "k"))
    return pytest.importorskip("compare_swing_v2")


def test_gate_is_inconclusive_until_each_book_has_30_closed_trades(gate):
    few, enough, good = {"closed": 2, "avg_R": 5.0}, {"closed": 30, "avg_R": 0.1}, {"closed": 31, "avg_R": 0.2}
    assert gate.gate_verdict(enough, few) == "INCONCLUSIVE: closed trades v2 2/30"
    assert gate.gate_verdict(few, few) == "INCONCLUSIVE: closed trades v1 2/30, v2 2/30"
    assert gate.gate_verdict(enough, good).startswith("V2 PASSES")
    assert gate.gate_verdict(good, enough).startswith("KEEP V1")
    assert gate.gate_verdict(enough, enough).startswith("V2 PASSES")           # at least as good


# ── Weekly stages ──────────────────────────────────────────────────────────────

def test_swing_day_skips_the_weekly_stages_only():
    daily = default_stages(longterm=False, trading_mode="swing")
    assert not daily & WEEKLY_STAGES and {0, 1, 2, 3, 4, 8, 9, 35} <= daily


def test_saturday_and_position_mode_still_run_them():
    assert WEEKLY_STAGES <= default_stages(longterm=True, trading_mode="position")
    assert WEEKLY_STAGES <= default_stages(longterm=False, trading_mode="position")


def test_swing_change_section_shows_flips_only(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", os.environ.get("SUPABASE_URL", "http://x"))
    monkeypatch.setenv("SUPABASE_KEY", os.environ.get("SUPABASE_KEY", "k"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", "k"))
    nl = pytest.importorskip("run_newsletter")
    change = {"available": True, "flips_only": True, "prior_date": None, "flips": [], "entered": [], "exited": [], "deltas": []}
    for body in ("\n".join(nl._plaintext_change_lines(change)), nl.build_change_html(change)):
        assert "No signal flips." in body and "weight" not in body


# ── Fill-day stops in the simulation ──────────────────────────────────────────

def _bars():
    return pd.DataFrame([
        {"date": date(2026, 10, 12), "open": 101.0, "high": 101.0, "low": 95.0, "close": 99.0},    # fills 100, touches 96
        {"date": date(2026, 10, 13), "open": 99.0, "high": 109.0, "low": 98.0, "close": 108.0},    # would hit the target
    ])


PLAN = {"report_date": date(2026, 10, 12), "entry_price": 100.0, "stop_price": 96.0, "target_price": 108.0, "horizon_days": 5}


def test_fill_day_stop_scores_the_stop_that_day():
    t = simulate_trade(PLAN, _bars(), fill_day_stop=True)
    assert t["outcome"] == STOP and t["exit_date"] == date(2026, 10, 12) and t["hold_sessions"] == 0
    assert t["exit_price"] == 96.0 and t["r_multiple"] < -1.0


def test_default_simulation_is_unchanged():
    t = simulate_trade(PLAN, _bars())
    assert t["outcome"] == "TARGET" and t["exit_date"] == date(2026, 10, 13)
