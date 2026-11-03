from swing_llm_v2 import apply_decisions, news_read, parse_news


def cand(t, entry=100.0, stop=97.0, target=106.0, atr=2.0, news=None):
    return {"ticker": t, "setup_type": "TREND_PULLBACK", "entry_price": entry, "stop_price": stop,
            "target_price": target, "horizon_days": 7, "atr_14": atr, "risk_reward": 2.0,
            "news": news or {"catalyst": "NONE", "veto": False, "reason": ""}}


def test_model_cannot_add_tickers_or_change_entry_target():
    out = apply_decisions([cand("A")], [
        {"ticker": "A", "decision": "KEEP", "rank": 1, "entry": 90, "target": 200},
        {"ticker": "ZZZ", "decision": "KEEP", "rank": 1},
    ])
    assert [c["ticker"] for c in out] == ["A"]
    assert out[0]["entry_price"] == 100.0 and out[0]["target_price"] == 106.0


def test_stop_only_tightens_within_bounds():
    looser = apply_decisions([cand("A")], [{"ticker": "A", "decision": "KEEP", "stop": 95}])[0]
    assert looser["stop_price"] == 97.0
    too_tight = apply_decisions([cand("A")], [{"ticker": "A", "decision": "KEEP", "stop": 99.5}])[0]
    assert too_tight["stop_price"] == 97.0                      # < 0.5 ATR from entry
    tighter = apply_decisions([cand("A")], [{"ticker": "A", "decision": "KEEP", "stop": 98}])[0]
    assert tighter["stop_price"] == 98.0 and tighter["llm_stop_from"] == 97.0
    assert tighter["risk_reward"] == 3.0
    junk = apply_decisions([cand("A")], [{"ticker": "A", "decision": "KEEP", "stop": "n/a"}])[0]
    assert junk["stop_price"] == 97.0


def test_ordering_and_vetoes():
    out = apply_decisions([cand("A"), cand("B"), cand("C")], [
        {"ticker": "A", "decision": "VETO", "reason": "offering"},
        {"ticker": "B", "decision": "KEEP", "rank": 2},
        {"ticker": "C", "decision": "KEEP", "rank": 1},
    ])
    assert [(c["ticker"], c["llm_decision"]) for c in out] == [("C", "KEEP"), ("B", "KEEP"), ("A", "VETO")]
    assert [c["script_rank"] for c in out] == [2, 1, 0]


def test_news_veto_overrides_keep_and_missing_reply_keeps():
    out = apply_decisions([cand("A", news={"catalyst": "BINARY_EVENT", "veto": True, "reason": "FDA ruling Tue"}),
                           cand("B")], [{"ticker": "A", "decision": "KEEP", "rank": 1}])
    a = next(c for c in out if c["ticker"] == "A")
    b = next(c for c in out if c["ticker"] == "B")
    assert a["llm_decision"] == "VETO" and "FDA" in a["llm_reason"]
    assert b["llm_decision"] == "KEEP" and "not covered" in b["llm_reason"]


def test_parse_news_tolerates_prose_and_bad_values():
    d = parse_news('Here you go: {"catalyst": "weird", "veto": 1, "reason": "x"} thanks')
    assert d == {"catalyst": "NONE", "veto": True, "reason": "x"}


def test_news_read_skips_call_without_headlines():
    assert news_read("A", None, [])["tokens"] == 0
