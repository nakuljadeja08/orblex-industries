"""
swing_screen.py — PIPELINE_V2 swing candidates from rules alone (EXECUTOR_DESIGN.md §3, §4).

In v1 the analyst model reads every ticker's numbers and proposes BUY/SELL/HOLD
with levels, and swing_policy clamps them. In v2 a script makes the call and
sets the levels; the model (swing_llm_v2.py) only reads news for the few
candidates that pass, and may veto or tighten, never add or loosen.

Pure functions, no I/O, so the live stage and the historical replay
(swing_screen_backtest.py) run exactly the same rules.

Long setup
  TREND_PULLBACK   the mechanical baseline, BULL regime only: trend UP (close > MA20 > MA50),
                   limit at close − 0.5 ATR, stop 1.5 ATR under the entry,
                   target 3 ATR over it (2R), 7-session horizon. Optional
                   filters (FILTERS) are switched on only when the replay
                   shows they add expectancy.

Inverse setups (decision 4): ordinary long buys of an inverse ETF
  BEAR_TREND       regime CORRECTION/BEAR and the inverse ETF itself in an
                   uptrend: same levels as TREND_PULLBACK, 5-session horizon.
  DOWN_DAY         regime BULL/CHOPPY but the index is breaking down: close
                   below its MA20 AND below the prior 10-session low, on
                   volume ≥ 1.2× average. Limit at close + 0.1 ATR, stop 1 ATR,
                   target 1.5R, 3-session horizon, half risk.
2x inverse ETFs always carry half the risk of the 1x setup. No 3x.
"""

from typing import Optional

import pandas as pd

from swing_policy import compute_swing_indicators

# ── Long setup ────────────────────────────────────────────────────────────────
ENTRY_BELOW_CLOSE_ATR = 0.5
STOP_ATR              = 1.5
TARGET_ATR            = 3.0
LONG_HORIZON          = 7
# Longs only in a BULL regime: the 2022–2026 replay lost money on longs in CHOPPY
# and CORRECTION under the account limits (+0.013R/trade all regimes vs +0.093R
# BULL-only, n=435 vs 315). Unknown regime fails closed.
LONG_REGIMES          = {"BULL"}

# Optional long filters, named so the replay can switch them on one at a time.
FILTERS = {
    "rs_positive":   lambda i: (i.get("rs_vs_spy_20d_pct") or 0) > 0,           # beating SPY over 20d
    "not_extended":  lambda i: i.get("price_vs_ma20_pct") is not None and i.get("atr_pct")
                               and i["price_vs_ma20_pct"] <= 2.5 * i["atr_pct"],  # ≤ 2.5 ATR above MA20
    "rsi_not_hot":   lambda i: (i.get("rsi_14") or 0) < 70,
    "ma20_rising":   lambda i: (i.get("ma20_slope_5d_pct") or 0) > 0,
}

# ── Inverse setups ────────────────────────────────────────────────────────────
INVERSE_ETFS = {   # ticker → (index it tracks inversely, leverage)
    "SH":  ("SPY", 1), "PSQ": ("QQQ", 1),
    "SDS": ("SPY", 2), "QID": ("QQQ", 2),
}
BEAR_REGIMES        = {"CORRECTION", "BEAR"}
BEAR_TREND_HORIZON  = 5
DOWN_DAY_REGIMES    = {"BULL", "CHOPPY"}
DOWN_DAY_LOOKBACK   = 10
DOWN_DAY_VOL_RATIO  = 1.2
DOWN_DAY_ENTRY_ATR  = 0.1
DOWN_DAY_STOP_ATR   = 1.0
DOWN_DAY_TARGET_R   = 1.5
DOWN_DAY_HORIZON    = 3


def _plan(ticker, setup, entry, stop, target, horizon, risk_mult, rank_score, ind, reasons):
    return {
        "ticker": ticker, "setup_type": setup,
        "entry_price": round(entry, 2), "stop_price": round(stop, 2), "target_price": round(target, 2),
        "horizon_days": horizon, "risk_mult": risk_mult, "rank_score": rank_score,
        "risk_reward": round((target - entry) / (entry - stop), 2),
        "latest_close": ind.get("latest_close"), "atr_14": ind.get("atr_14"),
        "reasons": reasons,
    }


def screen_long(ticker: str, ind: dict, regime: Optional[str],
                filters: tuple[str, ...] = ()) -> tuple[Optional[dict], str]:
    """(plan, "") for a TREND_PULLBACK candidate, else (None, why not)."""
    if not ind or ind.get("atr_14") in (None, 0) or ind.get("latest_close") is None:
        return None, "not enough history"
    if regime not in LONG_REGIMES:
        return None, f"regime {regime}: longs only in {'/'.join(sorted(LONG_REGIMES))}"
    if ind.get("trend") != "UP":
        return None, f"trend {ind.get('trend')}"
    for f in filters:
        if not FILTERS[f](ind):
            return None, f"filter {f}"
    close, atr = ind["latest_close"], ind["atr_14"]
    entry = close - ENTRY_BELOW_CLOSE_ATR * atr
    stop = entry - STOP_ATR * atr
    target = entry + TARGET_ATR * atr
    risk_mult = 1.0
    reasons = [f"trend UP (close > MA20 > MA50)", f"RS vs SPY 20d {ind.get('rs_vs_spy_20d_pct')}%"]
    plan = _plan(ticker, "TREND_PULLBACK", entry, stop, target, LONG_HORIZON, risk_mult,
                 ind.get("rs_vs_spy_20d_pct") or 0.0, ind, reasons)
    # How far above MA20, in ATRs: the "pullback" ranking prefers the least stretched.
    pv, ap = ind.get("price_vs_ma20_pct"), ind.get("atr_pct")
    plan["ext_atr"] = round(pv / ap, 2) if pv is not None and ap else None
    return plan, ""


def prior_low(df: pd.DataFrame, n: int = DOWN_DAY_LOOKBACK) -> Optional[float]:
    """Lowest low of the n sessions BEFORE the latest bar."""
    if len(df) < n + 1:
        return None
    return float(df["low"].astype(float).iloc[-n - 1:-1].min())


def index_breaking_down(index_df: pd.DataFrame) -> tuple[bool, str]:
    """The DOWN_DAY trigger on the index's own bars (date-ascending, through the latest close)."""
    ind = compute_swing_indicators(index_df)
    if not ind or ind.get("ma_20") is None:
        return False, "not enough history"
    low10 = prior_low(index_df)
    close = ind["latest_close"]
    vr = ind.get("volume_ratio") or 0
    if close >= ind["ma_20"]:
        return False, "index above MA20"
    if low10 is None or close >= low10:
        return False, "index above its prior 10-session low"
    if vr < DOWN_DAY_VOL_RATIO:
        return False, f"volume {vr}× < {DOWN_DAY_VOL_RATIO}×"
    return True, f"index closed below MA20 and the prior {DOWN_DAY_LOOKBACK}-session low on {vr}× volume"


def screen_inverse(ticker: str, inv_ind: dict, regime: Optional[str],
                   index_df: pd.DataFrame) -> tuple[Optional[dict], str]:
    """(plan, "") for a BEAR_TREND or DOWN_DAY trade in an inverse ETF, else (None, why not)."""
    if ticker not in INVERSE_ETFS:
        return None, "not an inverse ETF"
    if not inv_ind or inv_ind.get("atr_14") in (None, 0) or inv_ind.get("latest_close") is None:
        return None, "not enough history"
    _, lev = INVERSE_ETFS[ticker]
    lev_mult = 1.0 if lev == 1 else 0.5
    close, atr = inv_ind["latest_close"], inv_ind["atr_14"]

    if regime in BEAR_REGIMES:
        if inv_ind.get("trend") != "UP":
            return None, f"regime {regime} but {ticker} trend {inv_ind.get('trend')}"
        entry = close - ENTRY_BELOW_CLOSE_ATR * atr
        stop, target = entry - STOP_ATR * atr, entry + TARGET_ATR * atr
        return _plan(ticker, "BEAR_TREND", entry, stop, target, BEAR_TREND_HORIZON, lev_mult,
                     inv_ind.get("ret_20d_pct") or 0.0, inv_ind, [f"regime {regime}", f"{ticker} trend UP"]), ""

    if regime in DOWN_DAY_REGIMES or regime is None:
        ok, why = index_breaking_down(index_df)
        if not ok:
            return None, why
        entry = close + DOWN_DAY_ENTRY_ATR * atr
        stop = entry - DOWN_DAY_STOP_ATR * atr
        target = entry + DOWN_DAY_TARGET_R * (entry - stop)
        return _plan(ticker, "DOWN_DAY", entry, stop, target, DOWN_DAY_HORIZON, 0.5 * lev_mult,
                     -(compute_swing_indicators(index_df).get("day_change_pct") or 0.0), inv_ind, [why]), ""

    return None, f"regime {regime}"


RANKINGS = ("rs", "pullback")


def rank(plans: list[dict], by: str = "rs") -> list[dict]:
    """Best first. "rs": strongest 20d relative strength (inverse: their own score).
    "pullback": least stretched above MA20 in ATRs (inverse keep their own score)."""
    if by == "pullback":
        return sorted(plans, key=lambda p: (p["ext_atr"] if p.get("ext_atr") is not None
                                            else -(p.get("rank_score") or 0.0)))
    return sorted(plans, key=lambda p: -(p.get("rank_score") or 0.0))
