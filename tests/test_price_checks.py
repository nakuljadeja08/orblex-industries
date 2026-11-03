"""Tests for price_checks and its use in the plan builder. Fixtures use the real
get_equity_quotes response shape (data.results[].quote + .close)."""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from price_checks import _ts, close_issue, live_note, parse_quotes  # noqa: E402
from trade_guard import GuardContext  # noqa: E402
from trade_plan import build_entries  # noqa: E402

MON = date(2026, 10, 12)


def rh(symbol, last, close, close_date="2026-10-09", ext=None, ext_at=None, state="active"):
    return {"quote": {"symbol": symbol, "last_trade_price": str(last), "venue_last_trade_time": "2026-10-09T19:59:59Z",
                      "last_non_reg_trade_price": ext and str(ext), "venue_last_non_reg_trade_time": ext_at,
                      "previous_close": str(close), "previous_close_date": close_date,
                      "bid_price": str(last - 0.05), "ask_price": str(last + 0.05), "has_traded": True, "state": state},
            "close": {"symbol": symbol, "date": close_date, "price": str(close), "interpolated": False}}


def test_parses_real_shape_and_prefers_newer_extended_hours_trade():
    q = parse_quotes({"data": {"results": [rh("GOOG", 347.86, 347.86, ext=351.2, ext_at="2026-10-12T12:45:00Z")]}})
    assert q["GOOG"]["live"] == 351.2 and q["GOOG"]["official_close"] == 347.86 and q["GOOG"]["close_date"] == "2026-10-09"


def test_merges_multiple_responses():
    q = parse_quotes({"data": {"results": [rh("A1", 10, 10)]}}, {"data": {"results": [rh("B2", 20, 20)]}})
    assert set(q) == {"A1", "B2"}


def test_close_check():
    q = parse_quotes({"data": {"results": [rh("GOOG", 350, 347.86)]}})["GOOG"]
    assert close_issue(347.86, "2026-10-09", q) is None
    assert close_issue(348.5, "2026-10-09", q) is None                       # +0.18%: within tolerance
    assert "tolerance" in close_issue(352.0, "2026-10-09", q)                # +1.2%: bad bar
    assert "2026-10-08" in close_issue(347.86, "2026-10-08", q)              # stale bar
    assert close_issue(347.86, "2026-10-09", None) is not None               # no quote: fail closed


def test_live_note():
    q = {"live": 362.1, "live_at": "2026-10-12T12:45:00+00:00"}
    assert "above limit" in live_note({"limit_price": 355.25, "stop_price": 342.5}, q)
    assert "would fill now" in live_note({"limit_price": 365.0, "stop_price": 342.5}, q)


def ctx():
    return GuardContext(today=MON, equity=5000.0, buying_power=5000.0, positions={}, bought_today=set(),
                        sold_today=set(), open_orders=[], universe={"GOOG", "AVGO", "PATH"})


def plan(t, e, s, tg):
    return {"run_id": "r", "ticker": t, "report_date": MON, "signal_strength": "STRONG", "setup_type": "PULLBACK",
            "entry_price": e, "stop_price": s, "target_price": tg, "horizon_days": 5, "risk_reward": 2.0}


def test_plan_blocks_on_bad_close_missing_quote_and_live_below_stop():
    quotes = parse_quotes({"data": {"results": [
        rh("GOOG", 350.21, 347.86),            # good
        rh("AVGO", 356.37, 361.54),            # stored close disagrees below
        rh("PATH", 12.0, 13.48),               # live already below stop
    ]}})
    stored = {"GOOG": (347.86, "2026-10-09"), "AVGO": (355.10, "2026-10-09"), "PATH": (13.48, "2026-10-09")}
    plans = [plan("GOOG", 343.5, 336.5, 358.0), plan("AVGO", 355.25, 342.5, 379.0),
             plan("PATH", 13.2, 12.6, 14.4), plan("MU", 100, 96, 108)]
    out = {o["ticker"]: o for o in build_entries(ctx(), plans, {}, "LOW", quotes, stored)}
    assert out["GOOG"]["status"] == "PROPOSED" and "live 350.21" in out["GOOG"]["note"]
    assert "BAD_PRICE_DATA" in out["AVGO"]["guard_reasons"]
    assert "QUOTE_BELOW_STOP" in out["PATH"]["guard_reasons"]
    assert "BAD_PRICE_DATA" in out["MU"]["guard_reasons"]       # no quote at all: fail closed


def test_not_trading_blocks():
    quotes = parse_quotes({"data": {"results": [rh("GOOG", 350.21, 347.86, state="inactive")]}})
    out = build_entries(ctx(), [plan("GOOG", 343.5, 336.5, 358.0)], {}, "LOW", quotes, {"GOOG": (347.86, "2026-10-09")})
    assert "NOT_TRADING" in out[0]["guard_reasons"]


def test_timestamps_with_any_fraction_length():
    # GTLB 2026-10-14: an 8-digit fraction failed to parse, so the pre-market
    # trade (49.55) lost to the prior session's last trade (51.87).
    for s in ("2026-10-14T13:22:22.73010827Z", "2026-10-14T13:22:35.493422055Z",
              "2026-10-14T01:51:28.108Z", "2026-10-14T13:22:22Z"):
        assert _ts(s) is not None, s
    q = parse_quotes({"data": {"results": [rh("GTLB", 51.95, 51.87, ext=49.546, ext_at="2026-10-14T13:22:22.73010827Z")]}})
    assert q["GTLB"]["live"] == 49.546
