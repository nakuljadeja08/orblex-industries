from datetime import date

import pytest

from build_longterm import build_advice, changes_since, decide, first_sentence, week_of


@pytest.mark.parametrize("target,current,expected", [
    (0, 0, "AVOID"), (5, 0, "NEW"), (0, 4, "EXIT"), (5, 4.5, "HOLD"), (5, 3, "ADD"), (3, 5, "TRIM"),
    (5, None, "NEW"),
])
def test_decide(target, current, expected):
    assert decide(target, current) == expected


def test_week_of_saturday_run_advises_next_week():
    assert week_of(date(2026, 10, 17)) == date(2026, 10, 19)    # Sat → next Mon
    assert week_of(date(2026, 10, 15)) == date(2026, 10, 12)    # Thu → this Mon


def test_first_sentence_trims():
    assert first_sentence("One. Two.") == "One."
    assert first_sentence("x" * 300).endswith("…") and len(first_sentence("x" * 300)) == 160
    assert first_sentence(None) is None


def det(t, pct, risk=4, strength="MODERATE", signal="BUY"):
    return {"ticker": t, "allocated_pct": pct, "risk_score": risk, "signal_strength": strength,
            "analyst_signal": signal, "sector": "TECH", "risk_flags": []}


def test_against_last_week_without_holdings():
    prev = {"A": {"ticker": "A", "target_pct": 4.0, "action": "NEW", "conviction": 2, "signal": "BUY"}}
    rows = build_advice("r", date(2026, 10, 17), {"A": {"signal": "BUY", "signal_strength": "STRONG"}},
                        [det("A", 7.0), det("B", 0.0, risk=9)], {}, {}, prev, None, "BULL", 20.0)
    a = next(r for r in rows if r["ticker"] == "A")
    b = next(r for r in rows if r["ticker"] == "B")
    assert a["basis"] == "LAST_WEEK" and a["action"] == "ADD" and a["conviction"] == 3
    assert "target 4.0% → 7.0%" in a["changes"] and "conviction up" in a["changes"]
    assert b["action"] == "AVOID" and b["changes"] == ["new this week"]
    assert [r["action"] for r in rows] == ["ADD", "AVOID"]     # actionable first


def test_against_holdings_and_exit_for_unplanned_holding():
    rows = build_advice("r", date(2026, 10, 17), {}, [det("A", 5.0), det("C", 0.0)], {}, {}, {},
                        {"A": 9.0, "C": 3.0, "ZZ": 2.0}, "BULL", 20.0)
    by = {r["ticker"]: r for r in rows}
    assert by["A"]["action"] == "TRIM" and by["A"]["basis"] == "HOLDINGS"
    assert by["C"]["action"] == "EXIT"
    assert by["ZZ"]["action"] == "EXIT" and by["ZZ"]["target_pct"] == 0.0


def test_why_is_capped_and_sourced():
    rep = {"signal": "BUY", "signal_strength": "MODERATE", "reasoning": "Trend is up. More.",
           "valuation_view": "UNDERVALUED"}
    rows = build_advice("r", date(2026, 10, 17), {"A": rep}, [det("A", 5.0)],
                        {"A": {"forward_pe": 20, "peg_ratio": 1.1}},
                        {"A": {"holders": 3, "period": "2026-06-30", "ADDED": 2}}, {}, None, "BULL", 20.0)
    why = rows[0]["why"]
    assert [w["source"] for w in why] == ["Analyst", "Valuation", "Smart money"]
    assert why[0]["text"] == "BUY MODERATE: Trend is up."
    assert "PEG 1.10" in why[1]["text"] and "2 added" in why[2]["text"]


def test_changes_since_action_and_signal_flip():
    row = {"action": "TRIM", "target_pct": 3.0, "conviction": 1, "signal": "SELL"}
    prev = {"action": "ADD", "target_pct": 3.2, "conviction": 2, "signal": "BUY"}
    assert changes_since(row, prev) == ["action ADD → TRIM", "conviction down", "signal BUY → SELL"]


def test_holdings_weights_and_missing_price():
    from save_holdings import weights
    pf = {"data": {"total_value": "1000"}}
    pos = {"data": {"positions": [{"symbol": "a", "quantity": "2"}, {"symbol": "B", "quantity": "0"}]}}
    assert weights(pf, pos, {"A": {"live": None, "official_close": 100.0}}) == {"A": 20.0}
    with pytest.raises(ValueError):
        weights(pf, pos, {})
    with pytest.raises(ValueError):
        weights({"data": {"total_value": "0"}}, pos, {"A": {"live": 1}})


def test_negative_pe_is_not_shown_as_a_valuation():
    rows = build_advice("r", date(2026, 10, 17), {"A": {"signal": "BUY", "valuation_view": "UNDERVALUED"}},
                        [det("A", 5.0)], {"A": {"forward_pe": -44.0, "peg_ratio": -2}}, {}, {}, None, "BULL", 20.0)
    v = next(w for w in rows[0]["why"] if w["source"] == "Valuation")
    assert "P/E" not in v["text"] and "not profitable" in v["text"]



def test_holding_without_a_quote_falls_back_to_cost_and_is_named():
    from save_holdings import weights
    pf = {"data": {"total_value": "1000"}}
    pos = {"data": {"positions": [{"symbol": "APLS", "quantity": "2", "average_buy_price": "50"}]}}
    at_cost = []
    assert weights(pf, pos, {}, at_cost) == {"APLS": 10.0} and at_cost == ["APLS"]
    with pytest.raises(ValueError):
        weights(pf, {"data": {"positions": [{"symbol": "X", "quantity": "1"}]}}, {}, [])   # no cost either



def test_inactive_instrument_is_left_out_and_named():
    from save_holdings import weights
    pf = {"data": {"total_value": "1000"}}
    pos = {"data": {"positions": [{"symbol": "APLS^", "quantity": "5", "average_buy_price": "0.000000"},
                                  {"symbol": "MSFT", "quantity": "1", "average_buy_price": "400"}]}}
    inactive = []
    assert weights(pf, pos, {"MSFT": {"live": 500.0}}, [], inactive) == {"MSFT": 50.0} and inactive == ["APLS^"]
