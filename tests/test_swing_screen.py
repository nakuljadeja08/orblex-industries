from datetime import date, timedelta

import pandas as pd
import pytest

from swing_screen import (
    DOWN_DAY_HORIZON, LONG_HORIZON, index_breaking_down, prior_low, rank, screen_inverse, screen_long,
)

UP = {"latest_close": 100.0, "atr_14": 2.0, "trend": "UP", "rs_vs_spy_20d_pct": 4.0,
      "price_vs_ma20_pct": 3.0, "atr_pct": 2.0, "rsi_14": 60, "ma20_slope_5d_pct": 1.0, "ret_20d_pct": 5.0}


def bars(closes, vols=None):
    d0 = date(2026, 1, 1)
    vols = vols or [1_000_000] * len(closes)
    return pd.DataFrame({
        "date": [d0 + timedelta(days=i) for i in range(len(closes))],
        "open": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
        "close": closes, "volume": vols,
    })


def test_long_levels_match_baseline():
    p, why = screen_long("X", UP, "BULL")
    assert why == ""
    assert p["entry_price"] == 99.0 and p["stop_price"] == 96.0 and p["target_price"] == 105.0
    assert p["risk_reward"] == 2.0 and p["horizon_days"] == LONG_HORIZON and p["risk_mult"] == 1.0


@pytest.mark.parametrize("trend", ["DOWN", "MIXED", None])
def test_long_needs_uptrend(trend):
    assert screen_long("X", {**UP, "trend": trend}, "BULL")[0] is None


@pytest.mark.parametrize("regime", ["BEAR", "CORRECTION", "CHOPPY", None])
def test_longs_only_in_bull(regime):
    assert screen_long("X", UP, regime)[0] is None


def test_pullback_rank_prefers_least_stretched():
    a = screen_long("A", {**UP, "price_vs_ma20_pct": 6.0, "rs_vs_spy_20d_pct": 9}, "BULL")[0]
    b = screen_long("B", {**UP, "price_vs_ma20_pct": 1.0, "rs_vs_spy_20d_pct": 1}, "BULL")[0]
    assert [p["ticker"] for p in rank([a, b], "pullback")] == ["B", "A"]
    assert [p["ticker"] for p in rank([a, b])] == ["A", "B"]


def test_long_filters():
    assert screen_long("X", {**UP, "rs_vs_spy_20d_pct": -1}, "BULL", ("rs_positive",))[0] is None
    assert screen_long("X", {**UP, "price_vs_ma20_pct": 6.0}, "BULL", ("not_extended",))[0] is None
    assert screen_long("X", {**UP, "rsi_14": 75}, "BULL", ("rsi_not_hot",))[0] is None
    assert screen_long("X", UP, "BULL", ("rs_positive", "not_extended", "rsi_not_hot", "ma20_rising"))[0]


def test_long_needs_history():
    assert screen_long("X", {}, "BULL")[0] is None
    assert screen_long("X", {**UP, "atr_14": 0}, "BULL")[0] is None


def test_prior_low_excludes_today():
    df = bars([10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10, 1])
    assert prior_low(df, 10) == pytest.approx(5 * 0.99)


def test_index_breakdown_trigger():
    calm = [100 + i * 0.1 for i in range(40)]
    crash = calm + [95.0]
    ok, _ = index_breaking_down(bars(crash, [1_000_000] * 40 + [2_000_000]))
    assert ok
    ok, why = index_breaking_down(bars(crash, [1_000_000] * 41))
    assert not ok and "volume" in why
    ok, _ = index_breaking_down(bars(calm))
    assert not ok


def test_inverse_bear_trend_and_leverage():
    p, _ = screen_inverse("PSQ", UP, "BEAR", bars([100] * 40))
    assert p["setup_type"] == "BEAR_TREND" and p["risk_mult"] == 1.0
    p2, _ = screen_inverse("QID", UP, "CORRECTION", bars([100] * 40))
    assert p2["risk_mult"] == 0.5
    assert screen_inverse("SH", {**UP, "trend": "DOWN"}, "BEAR", bars([100] * 40))[0] is None


def test_inverse_down_day_in_bull():
    crash = [100 + i * 0.1 for i in range(40)] + [95.0]
    idx = bars(crash, [1_000_000] * 40 + [2_000_000])
    p, _ = screen_inverse("SH", {**UP, "trend": "DOWN"}, "BULL", idx)
    assert p["setup_type"] == "DOWN_DAY" and p["horizon_days"] == DOWN_DAY_HORIZON and p["risk_mult"] == 0.5
    assert p["entry_price"] > UP["latest_close"]          # buys strength in the inverse
    p2, _ = screen_inverse("SDS", UP, "BULL", idx)
    assert p2["risk_mult"] == 0.25                        # 2x at half of the half-risk setup
    assert screen_inverse("SH", UP, "BULL", bars([100] * 41))[0] is None


def test_only_known_inverse_etfs():
    assert screen_inverse("SQQQ", UP, "BEAR", bars([100] * 40))[0] is None   # no 3x


def test_rank_orders_by_score():
    assert [p["rank_score"] for p in rank([{"rank_score": 1}, {"rank_score": 5}, {"rank_score": None}])] == [5, 1, None]
