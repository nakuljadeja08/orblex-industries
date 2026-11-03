"""
regime_detector.py — Orblex Industries Market Regime Detector.

Deterministic, non-LLM portfolio-level regime classification.

Philosophy (consistent with the rest of the pipeline):
  Hard constraints beat prompt guardrails. The regime is computed from
  benchmark data with explicit, auditable rules — no model call. Downstream,
  the Allocator treats the regime's risk_floor as a hard constraint when
  sizing the cash reserve.

Inputs (fetched fresh via yfinance — benchmarks are not in stock_prices):
  SPY   — broad market benchmark (primary)
  QQQ   — tech benchmark (confirmation; portfolio is tech-heavy)
  ^VIX  — implied volatility

Indicators:
  - SPY close vs 200-day MA (% distance)
  - SPY drawdown from 252-trading-day (52-week) high
  - SPY 50-day vs 200-day MA (death cross)
  - QQQ close vs 200-day MA
  - VIX close

Classification rules (first match wins, most severe first):

  BEAR        spy_drawdown <= -20%
              OR (spy below 200MA AND vix >= 30)
              OR (death_cross AND spy_drawdown <= -15%)

  CORRECTION  spy_drawdown <= -10%
              OR (spy below 200MA AND vix >= 25)

  CHOPPY      spy below 200MA
              OR vix >= 20
              OR spy_drawdown <= -5%
              OR qqq below 200MA          (tech-heavy portfolio sensitivity)

  BULL        otherwise

Regime → minimum market risk level (risk_floor) for the Allocator:

  BULL        None        (no escalation — Risk Manager's level stands)
  CHOPPY      MODERATE    (cash floor >= 10%)
  CORRECTION  HIGH        (cash floor >= 20%)
  BEAR        CRITICAL    (cash floor >= 35%, 10% single-position cap)

Note: the floor only ever ESCALATES risk. If the Risk Manager already says
CRITICAL and the regime is BULL, CRITICAL stands. max(), never min().
"""

import logging
from datetime import date
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

BENCHMARK   = "SPY"
TECH_BENCH  = "QQQ"
VIX_TICKER  = "^VIX"

# Regime → minimum market risk level the Allocator must respect.
RISK_FLOOR = {
    "BULL":       None,
    "CHOPPY":     "MODERATE",
    "CORRECTION": "HIGH",
    "BEAR":       "CRITICAL",
}

# Thresholds (single source of truth — referenced by rules AND rationale text)
DRAWDOWN_BEAR        = -20.0
DRAWDOWN_BEAR_CROSS  = -15.0
DRAWDOWN_CORRECTION  = -10.0
DRAWDOWN_CHOPPY      = -5.0
VIX_BEAR             = 30.0
VIX_CORRECTION       = 25.0
VIX_CHOPPY           = 20.0


# ── Data fetching ─────────────────────────────────────────────────────────────

def _fetch_history(ticker: str, period: str = "2y") -> Optional[pd.DataFrame]:
    """
    Fetch daily OHLCV history for a benchmark ticker.
    2y period ensures >252 rows for the 52-week high and 200-day MA.
    """
    try:
        df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
        if df is None or df.empty or len(df) < 210:
            logger.warning(f"  Insufficient history for {ticker} ({0 if df is None else len(df)} rows)")
            return None
        return df
    except Exception as e:
        logger.error(f"  Failed to fetch {ticker}: {e}")
        return None


# ── Indicator computation ─────────────────────────────────────────────────────

def _compute_benchmark_stats(df: pd.DataFrame) -> dict:
    """Compute close, MAs, % vs 200MA, drawdown from 52-week high, death cross."""
    close   = df["Close"]
    ma50    = close.rolling(50).mean().iloc[-1]
    ma200   = close.rolling(200).mean().iloc[-1]
    last    = close.iloc[-1]
    high52  = close.tail(252).max()

    return {
        "close":          round(float(last), 4),
        "ma50":           round(float(ma50), 4),
        "ma200":          round(float(ma200), 4),
        "vs_ma200_pct":   round((float(last) / float(ma200) - 1.0) * 100, 2),
        "drawdown_pct":   round((float(last) / float(high52) - 1.0) * 100, 2),
        "death_cross":    bool(ma50 < ma200),
    }


def _fetch_vix() -> Optional[float]:
    try:
        df = yf.Ticker(VIX_TICKER).history(period="5d")
        if df is None or df.empty:
            return None
        return round(float(df["Close"].iloc[-1]), 2)
    except Exception as e:
        logger.error(f"  Failed to fetch VIX: {e}")
        return None


# ── Classification ────────────────────────────────────────────────────────────

def _classify(spy: dict, qqq: Optional[dict], vix: Optional[float]) -> tuple[str, list[str]]:
    """
    Apply the regime rules. Returns (regime, rules_fired).
    Rules are evaluated most-severe-first; ALL matching rules are recorded
    for the audit trail, but the most severe regime wins.
    """
    rules: list[str] = []

    below_200   = spy["vs_ma200_pct"] < 0
    dd          = spy["drawdown_pct"]
    vix_val     = vix if vix is not None else 0.0
    qqq_below   = (qqq["vs_ma200_pct"] < 0) if qqq else False

    # Record every condition that fired (auditability)
    if below_200:                       rules.append("SPY_BELOW_200MA")
    if spy["death_cross"]:              rules.append("SPY_DEATH_CROSS")
    if dd <= DRAWDOWN_BEAR:             rules.append(f"SPY_DRAWDOWN<={DRAWDOWN_BEAR:.0f}%")
    elif dd <= DRAWDOWN_CORRECTION:     rules.append(f"SPY_DRAWDOWN<={DRAWDOWN_CORRECTION:.0f}%")
    elif dd <= DRAWDOWN_CHOPPY:         rules.append(f"SPY_DRAWDOWN<={DRAWDOWN_CHOPPY:.0f}%")
    if vix_val >= VIX_BEAR:             rules.append(f"VIX>={VIX_BEAR:.0f}")
    elif vix_val >= VIX_CORRECTION:     rules.append(f"VIX>={VIX_CORRECTION:.0f}")
    elif vix_val >= VIX_CHOPPY:         rules.append(f"VIX>={VIX_CHOPPY:.0f}")
    if qqq_below:                       rules.append("QQQ_BELOW_200MA")

    # BEAR
    if (
        dd <= DRAWDOWN_BEAR
        or (below_200 and vix_val >= VIX_BEAR)
        or (spy["death_cross"] and dd <= DRAWDOWN_BEAR_CROSS)
    ):
        return "BEAR", rules

    # CORRECTION
    if dd <= DRAWDOWN_CORRECTION or (below_200 and vix_val >= VIX_CORRECTION):
        return "CORRECTION", rules

    # CHOPPY
    if below_200 or vix_val >= VIX_CHOPPY or dd <= DRAWDOWN_CHOPPY or qqq_below:
        return "CHOPPY", rules

    return "BULL", rules


def _build_rationale(regime: str, spy: dict, qqq: Optional[dict], vix: Optional[float],
                     rules: list[str]) -> str:
    """Human-readable rule trace for the audit trail / dashboard / newsletter."""
    parts = [
        f"Regime classified as {regime}.",
        f"SPY is {spy['vs_ma200_pct']:+.1f}% vs its 200-day MA, "
        f"{spy['drawdown_pct']:+.1f}% from its 52-week high"
        + (", with a death cross (50MA < 200MA)" if spy["death_cross"] else "")
        + ".",
    ]
    if vix is not None:
        parts.append(f"VIX at {vix:.1f}.")
    if qqq:
        parts.append(f"QQQ is {qqq['vs_ma200_pct']:+.1f}% vs its 200-day MA.")
    parts.append(f"Rules fired: {', '.join(rules) if rules else 'none'}.")
    floor = RISK_FLOOR[regime]
    parts.append(
        f"Allocator risk floor: {floor or 'none (Risk Manager level stands)'}.")
    return " ".join(parts)


# ── Main entry point ──────────────────────────────────────────────────────────

def detect_regime() -> Optional[dict]:
    """
    Fetch benchmark data, classify the market regime, and return a full
    result dict ready for regime_saver. Returns None if SPY data is
    unavailable (QQQ / VIX failures degrade gracefully).
    """
    logger.info(f"  Fetching {BENCHMARK} history...")
    spy_df = _fetch_history(BENCHMARK)
    if spy_df is None:
        logger.error("  Cannot classify regime without SPY data.")
        return None
    spy = _compute_benchmark_stats(spy_df)

    logger.info(f"  Fetching {TECH_BENCH} history...")
    qqq_df = _fetch_history(TECH_BENCH)
    qqq = _compute_benchmark_stats(qqq_df) if qqq_df is not None else None

    logger.info("  Fetching VIX...")
    vix = _fetch_vix()

    regime, rules = _classify(spy, qqq, vix)
    rationale     = _build_rationale(regime, spy, qqq, vix, rules)

    return {
        "regime_date":       date.today().isoformat(),
        "regime":            regime,
        "risk_floor":        RISK_FLOOR[regime],
        "spy_close":         spy["close"],
        "spy_ma50":          spy["ma50"],
        "spy_ma200":         spy["ma200"],
        "spy_vs_ma200_pct":  spy["vs_ma200_pct"],
        "spy_drawdown_pct":  spy["drawdown_pct"],
        "death_cross":       spy["death_cross"],
        "qqq_vs_ma200_pct":  qqq["vs_ma200_pct"] if qqq else None,
        "vix_close":         vix,
        "rationale":         rationale,
        "rules_fired":       rules,
    }