"""Tests for universe_selector — the account-balance-driven swing universe."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from universe_selector import equity_for_one_share, max_shares, select_universe  # noqa: E402


def m(price, atr_pct=3.0, dollar_vol=100e6, trend="UP", vs50=5.0, rs20=2.0):
    return {"price": price, "atr": price * atr_pct / 100, "atr_pct": atr_pct, "dollar_vol": dollar_vol,
            "trend": trend, "price_vs_ma50_pct": vs50, "rs20": rs20}


def by(rows):
    return {r["ticker"]: r for r in rows}


def test_max_shares_matches_trade_plan_sizing():
    # RISK_PER_TRADE_PCT 2.5: $500 × 2.5% × 0.75 = $9.38 risk; 1.5 × ATR($0.40) = $0.60/share → 15;
    # the 30% cap ($150 / $13.45) → 11, so the cap binds. At $250: risk → 7, cap → 5.
    assert max_shares(13.45, 0.40, 500) == 11
    assert max_shares(13.45, 0.40, 250) == 5
    assert max_shares(343.5, 7.0, 500) == 0
    assert round(equity_for_one_share(343.5, 7.0)) == 1145     # the cap: $343.50 / 30% (risk alone needs $560)


def test_no_balance_keeps_full_core_and_no_sleeve():
    rows = select_universe(["NVDA", "PATH"], {"VRT": "INFRASTRUCTURE"}, set(), {}, None)
    assert [r["ticker"] for r in rows if r["included"]] == ["NVDA", "PATH"]
    assert all(r["source"] != "SLEEVE" for r in rows)


def test_core_filtered_by_balance_and_held_always_kept():
    metrics = {"NVDA": m(230), "PATH": m(13.45), "MU": m(1029)}
    rows = by(select_universe(["NVDA", "PATH", "MU"], {}, {"MU"}, metrics, 500))
    assert rows["PATH"]["included"] and not rows["NVDA"]["included"]
    assert "account" in rows["NVDA"]["reason"]                       # "1 share needs a $X account"
    assert rows["MU"]["included"] and rows["MU"]["source"] == "HELD"  # held overrides sizing


def test_core_without_data_is_kept():
    assert by(select_universe(["XYZ"], {}, set(), {}, 500))["XYZ"]["included"]


def test_sleeve_screen_and_ranking():
    cands = {t: "SOFTWARE" for t in ("A1", "B2", "C3", "PENNY", "THIN", "WILD", "DOWN", "BIG")}
    metrics = {
        "A1": m(20, rs20=5.0), "B2": m(25, rs20=9.0), "C3": m(30, rs20=1.0),
        "PENNY": m(3), "THIN": m(20, dollar_vol=5e6), "WILD": m(20, atr_pct=9),
        "DOWN": m(20, trend="DOWN"), "BIG": m(900),
    }
    rows = by(select_universe([], cands, set(), metrics, 500, sleeve_size=2))
    assert [t for t in ("B2", "A1") if rows[t]["included"]] == ["B2", "A1"]   # top-2 by RS
    assert not rows["C3"]["included"] and rows["C3"]["rank"] == 3
    for t, word in (("PENNY", "price"), ("THIN", "thin"), ("WILD", "volatile"), ("DOWN", "uptrend"), ("BIG", "account")):
        assert not rows[t]["included"] and word in rows[t]["reason"], t


def test_candidates_already_in_core_are_not_double_counted():
    rows = select_universe(["PATH"], {"PATH": "SOFTWARE"}, set(), {"PATH": m(13.45)}, 500)
    assert [r["source"] for r in rows if r["ticker"] == "PATH"] == ["CORE"]


def test_sleeve_sector_cap():
    cands = {"S1": "SOFTWARE", "S2": "SOFTWARE", "S3": "SOFTWARE", "P1": "POWER"}
    metrics = {"S1": m(20, rs20=9), "S2": m(20, rs20=8), "S3": m(20, rs20=7), "P1": m(20, rs20=1)}
    rows = by(select_universe([], cands, set(), metrics, 500, sleeve_size=3, max_per_sector=2))
    assert [t for t in ("S1", "S2", "S3", "P1") if rows[t]["included"]] == ["S1", "S2", "P1"]
    assert "SOFTWARE already has 2" in rows["S3"]["reason"]


def test_total_cap_shrinks_sleeve_but_never_drops_held():
    core = [f"C{i}" for i in range(4)]
    metrics = {t: m(20) for t in core} | {"X1": m(20, rs20=5), "X2": m(20, rs20=4)}
    rows = select_universe(core, {"X1": "POWER", "X2": "SEMIS"}, {"H1"}, metrics, 5000, max_tickers=5)
    included = [r["ticker"] for r in rows if r["included"]]
    assert set(core) <= set(included) and "H1" in included and "X1" not in included   # 5 already → no sleeve
