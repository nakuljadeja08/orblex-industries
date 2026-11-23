"""
The public playground: the scenario engine on the real rules (with a fake analyst, so no Claude call),
and the server function's key and scenario checks.
"""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import playground as pg  # noqa: E402
from swing_policy import enforce_swing_rules  # noqa: E402

TODAY = date(2026, 11, 3)


def buy(ctx):
    sw = ctx["swing"]
    c, atr = sw["latest_close"], sw["atr_14"]
    a = {"signal": "BUY", "signal_strength": "MODERATE", "setup_type": "PULLBACK", "entry_price": round(c - 0.3 * atr, 2),
         "stop_price": round(c - 1.8 * atr, 2), "target_price": round(c + 2.5 * atr, 2), "horizon_days": 7, "risk_reward": 2.0}
    return enforce_swing_rules(a, sw, ctx["earnings"], today=TODAY)


def test_bars_match_the_scenario_exactly_and_repeat():
    r = pg.run({"price": 42, "trend_20d_pct": 10, "move_5d_pct": -4}, buy, TODAY)
    i = r["indicators"]
    assert i["latest_close"] == 42.0 and abs(i["ret_20d_pct"] - 10) < 0.1 and abs(i["ret_5d_pct"] + 4) < 0.1
    assert pg.run({"price": 42, "trend_20d_pct": 10, "move_5d_pct": -4}, buy, TODAY)["bars"] == r["bars"]


def test_scenario_is_clamped_and_trimmed():
    s = pg.clean({"price": 1e9, "atr_pct": -5, "ticker": "acme$$$robotics", "regime": "MOON",
                  "headlines": ["a" * 500, "", "b", "c", "d"], "days_to_earnings": "x", "extra": "ignored"})
    assert s["price"] == 2000.0 and s["atr_pct"] == 0.5 and s["ticker"] == "ACMER" and s["regime"] == "BULL"
    assert len(s["headlines"]) == 3 and len(s["headlines"][0]) == 200 and s["days_to_earnings"] is None
    assert "extra" not in s


def test_a_clean_buy_is_sized_and_proposed():
    r = pg.run(pg.PRESETS["Clean pullback"], buy, TODAY)
    assert r["analyst"]["signal"] == "BUY" and r["order"]["status"] == "PROPOSED" and r["order"]["qty"] > 0
    assert r["order"]["risk_dollars"] <= 5000 * 0.025 + 1e-6


def test_bear_market_blocks_every_entry():
    r = pg.run(pg.PRESETS["Bear market"], buy, TODAY)
    assert r["level"] == "CRITICAL" and r["order"]["status"] == "BLOCKED"
    assert "RISK_LEVEL_CRITICAL" in r["order"]["guard_reasons"]


def test_earnings_inside_the_hold_shortens_or_blocks_it():
    r = pg.run({**pg.PRESETS["Earnings next week"], "days_to_earnings": 4}, buy, TODAY)
    assert r["analyst"]["swing_flags"] and (r["order"] is None or r["order"]["horizon_days"] < 7)


def test_hold_has_no_order():
    r = pg.run({}, lambda ctx: {"signal": "HOLD", "signal_strength": "WEAK", "setup_type": "NONE"}, TODAY)
    assert r["order"] is None


def test_unreadable_answer_is_reported():
    with pytest.raises(pg.ScenarioError):
        pg.run({}, lambda ctx: None, TODAY)


# ── The server function ──────────────────────────────────────────────────────

spec = importlib.util.spec_from_file_location("api_playground", ROOT / "api" / "playground.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

KEY = "sk-ant-api03-" + "x" * 30


def call(key=KEY, body=b'{"price": 42}'):
    return api.serve(key, body, run=lambda s: pg.run(s, buy, TODAY))


def test_run_returns_the_result_without_the_key():
    status, out = call()
    assert status == 200 and out["result"]["order"]["status"] == "PROPOSED"
    assert KEY not in json.dumps(out)


def test_key_and_a_readable_scenario_are_required():
    assert call(key="")[0] == 400
    assert call(key="not-a-key")[0] == 400
    assert call(body=b"[1, 2]")[0] == 400
    assert call(body=b"{" + b" " * api.MAX_BODY + b"}")[0] == 400
