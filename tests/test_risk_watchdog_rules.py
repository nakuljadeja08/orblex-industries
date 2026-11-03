import pytest

from risk_rules import score_risk, score_ticker, worst_callers
from watchdog_rules import audit, cash_in_note, expected_stance


def t(ticker="X", signal="HOLD", strength="WEAK", vol=0.30, anomalies=1, **kw):
    return {"ticker": ticker, "signal": signal, "signal_strength": strength, "volatility": vol,
            "anomaly_count": anomalies, "anomalies": [], **kw}


@pytest.mark.parametrize("vol,expected", [(0.15, 3), (0.25, 3), (0.30, 5), (0.45, 5), (0.55, 7), (0.58, 8), (0.70, 8), (0.95, 9)])
def test_volatility_base(vol, expected):
    assert score_ticker(t(vol=vol), None, set())["risk_score"] == expected


def test_anomalies_signal_earnings_regime():
    assert score_ticker(t(anomalies=0), None, set())["risk_score"] == 4
    d = score_ticker(t(anomalies=4), None, set())
    assert d["risk_score"] == 6 and "ANOMALY_CLUSTER" in d["risk_flags"]
    assert score_ticker(t(signal="SELL", strength="MODERATE"), None, set())["risk_score"] == 6
    assert score_ticker(t(signal="SELL", strength="STRONG"), None, set())["risk_score"] == 7
    assert score_ticker(t(days_to_earnings=3), None, set())["risk_score"] == 6
    assert score_ticker(t(days_to_earnings=12), None, set())["risk_score"] == 5
    assert score_ticker(t(), "CORRECTION", set())["risk_score"] == 6
    assert score_ticker(t(), "BEAR", set())["risk_score"] == 7


def test_score_is_clamped():
    assert score_ticker(t(vol=2.0, anomalies=5, signal="SELL", strength="STRONG", days_to_earnings=1), "BEAR", {"X"})["risk_score"] == 10
    assert score_ticker(t(vol=0.10, anomalies=0), None, set())["risk_score"] == 2


def test_caps_follow_score_and_compress_in_bad_regimes():
    assert score_ticker(t(vol=0.10, anomalies=0), None, set())["max_position_pct"] == 25.0
    assert score_ticker(t(vol=0.70), None, set())["max_position_pct"] == 5.0
    assert score_ticker(t(signal="SELL", strength="WEAK", vol=0.10), None, set())["max_position_pct"] == 10.0
    assert score_ticker(t(vol=0.10, anomalies=0), "CORRECTION", set())["max_position_pct"] == 15.0   # score 2→3 (cap 20), ×0.75
    assert score_ticker(t(vol=0.10), "BEAR", set())["max_position_pct"] == 7.0              # score 3→5 (cap 14), ×0.5


def test_flags_without_score_change():
    d = score_ticker(t(signal="BUY", iv_rank=90, flow_signal="BEARISH",
                       anomalies=1, **{}) | {"anomalies": [{"type": "overbought"}, {"type": "large_price_move"}]},
                     None, set())
    assert d["risk_score"] == 5
    assert {"HIGH_IV", "SIGNAL_CONFLICT", "OVERBOUGHT", "BULL_TRAP_RISK"} <= set(d["risk_flags"])


def test_low_hit_rate_caps_and_scores():
    perf = {"hit_rates": {"X": {20: {"rate": 0.3, "n": 8}}, "Y": {20: {"rate": 0.3, "n": 2}}}}
    assert worst_callers(perf) == {"X"}
    d = score_ticker(t(vol=0.10, anomalies=0), None, {"X"})
    assert "LOW_HIT_RATE" in d["risk_flags"] and d["risk_score"] == 3 and d["max_position_pct"] == 10.0


def ctx(inputs, regime=None, floor=None, macro=None):
    sc = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for i in inputs:
        sc[i["signal"]] += 1
    vols = [i["volatility"] for i in inputs if i["volatility"] is not None]
    return {"ticker_inputs": inputs, "total_tickers": len(inputs), "signal_counts": sc,
            "avg_volatility": sum(vols) / len(vols) if vols else None,
            "market_context": {"regime": {"regime": regime, "risk_floor": floor}, "macro": macro or {}}}


def test_portfolio_levels_and_shape():
    out = score_risk(ctx([t("A", vol=0.15), t("B", vol=0.15)]))
    assert out["market_risk_level"] == "LOW" and out["coverage_complete"] and out["model"] == "rules-v2"
    assert len(out["ticker_details"]) == 2 and out["prompt_tokens"] == 0
    assert score_risk(ctx([t("A", vol=0.5), t("B", vol=0.5)]))["market_risk_level"] == "HIGH"
    assert score_risk(ctx([t("A", signal="SELL"), t("B", signal="SELL"), t("C")]))["market_risk_level"] == "CRITICAL"
    assert score_risk(ctx([t("A", vol=0.15)], floor="HIGH"))["market_risk_level"] == "HIGH"
    assert score_risk(ctx([t("A", vol=0.15)], macro={"equity_impact": "NEGATIVE"}))["market_risk_level"] == "MODERATE"
    assert score_risk(ctx([t("A", vol=0.15)]), {"drawdown": {"current_drawdown_pct": -12}})["market_risk_level"] == "HIGH"


def test_flagged_list_matches_scores():
    out = score_risk(ctx([t("A", vol=0.15, anomalies=0), t("B", vol=0.7)]))
    assert out["flagged_tickers"] == ["B"]


# ── watchdog ────────────────────────────────────────────────────────────────────

def row(tk, sig="HOLD", strength="WEAK", risk=4, cap=15, alloc=5.0, flags=()):
    return {"ticker": tk, "analyst_signal": sig, "signal_strength": strength, "risk_score": risk,
            "max_position_pct": cap, "allocated_pct": alloc, "risk_flags": list(flags), "sizing_method": "X"}


def wctx(cross, checks=None, **kw):
    base = {"consistency_checks": checks if checks is not None else [{"rule": "R", "status": "PASS", "detail": "ok"}],
            "ticker_cross": cross, "market_risk_level": "MODERATE", "portfolio_stance": "BALANCED",
            "cash_pct": 20.0, "invested_pct": 80.0, "flagged_tickers": [], "alloc_strategy_note": "",
            "alloc_run_id": "a", "risk_run_id": "r", "analyst_run_id": "n", "active_count": 1, "excluded_count": 0}
    return {**base, **kw}


def test_clean_run_scores_ten():
    r = audit(wctx([row("A")]))
    assert r["overall_status"] == "CLEAN" and r["pipeline_health_score"] == 10 and r["model"] == "rules-v1"


def test_fail_check_caps_health_at_three():
    r = audit(wctx([row("A")], checks=[{"rule": "SELL_TICKERS_EXCLUDED", "status": "FAIL", "detail": "x"}]))
    assert r["overall_status"] == "VIOLATIONS" and r["pipeline_health_score"] <= 3
    assert any("SELL" in rec for rec in r["recommendations"])


def test_issue_detection():
    cross = [row("A", sig="BUY", strength="STRONG", risk=4, alloc=0.0),
             row("B", alloc=12.0, cap=10),
             row("C", alloc=11.0, cap=20, flags=["ANOMALY_CLUSTER"]),
             row("D", alloc=0.0, flags=["ASSESSMENT_MISSING"], risk=8, cap=0)]
    kinds = {(i["issue_type"], i["ticker"]) for i in audit(wctx(cross, flagged_tickers=["D"]))["agent_consistency_issues"]}
    assert ("SIGNAL_ALLOCATION_MISMATCH", "A") in kinds and ("CAP_VIOLATION", "B") in kinds
    assert ("ANOMALY_IGNORED", "C") in kinds and ("MISSING_TICKER", "D") in kinds


def test_stance_rules():
    c = wctx([row("A"), row("B")])
    assert expected_stance(c) == "BALANCED"
    assert expected_stance({**c, "cash_pct": 30}) == "CASH_HEAVY"
    assert expected_stance({**c, "flagged_tickers": ["A"]}) == "DEFENSIVE"
    assert expected_stance({**c, "market_risk_level": "LOW", "cash_pct": 5,
                            "ticker_cross": [row("A", sig="BUY"), row("B", sig="BUY")]}) == "AGGRESSIVE"
    issues = audit({**c, "portfolio_stance": "CASH_HEAVY"})["agent_consistency_issues"]
    assert any(i["issue_type"] == "STANCE_MISMATCH" for i in issues)


def test_cash_note():
    assert cash_in_note("the 10% cash floor held") is None
    assert cash_in_note("holding 22.5% cash after trims") == 22.5
    assert cash_in_note("cash reserve of 18%") == 18.0
    assert cash_in_note("12% cash, then 15% cash") is None          # ambiguous → no claim
    issues = audit(wctx([row("A")], alloc_strategy_note="We hold 10% cash."))["agent_consistency_issues"]
    assert any("note says 10% cash" in i["description"] for i in issues)


def test_flagged_list_disagreement_is_low():
    issues = audit(wctx([row("A", risk=7, cap=8)], flagged_tickers=[]))["agent_consistency_issues"]
    assert [(i["issue_type"], i["severity"]) for i in issues] == [("OTHER", "LOW")]
