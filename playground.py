"""
playground.py — runs one made-up scenario through the real system, end to end, for the public
playground (EXECUTOR_DESIGN.md going-public phase 3):

  scenario   a visitor's settings: price, 20-day trend, last-5-day move, volatility, volume,
             earnings date, market regime and up to 3 headlines of their own
  bars       ~130 synthetic daily bars that end exactly at that price and match those settings
  analyst    the same swing prompt and Claude call as the pipeline (analyst_agent.analyse), with the
             visitor's own key, then swing_policy.enforce_swing_rules
  risk       risk_rules.score_ticker, the same score and cap as the daily risk stage
  sizing     trade_plan.build_entries: size, then every trade_guard check, on a paper account

Every step is returned, so the page can show one trade's path. Nothing is stored here, and the key
is used for this call only: it never reaches the result, a log or the database.

Pure apart from the one Claude call, which run() takes as `analyst` so tests can pass a fake.
"""

import hashlib
import random
import re
from datetime import date, datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from context_builder import _compute_indicators, _detect_anomalies
from regime_detector import RISK_FLOOR
from risk_rules import score_ticker
from trade_guard import GuardContext
from trade_plan import build_entries, effective_level

NY = ZoneInfo("America/New_York")
N_BARS = 130
REGIMES = ("BULL", "CHOPPY", "CORRECTION", "BEAR")
SPY_DRIFT = {"BULL": 0.0008, "CHOPPY": 0.0, "CORRECTION": -0.0012, "BEAR": -0.002}   # per session

# name: (default, min, max). Out-of-range values are clamped, not refused.
LIMITS = {
    "price":            (50.0, 1.0, 2000.0),
    "trend_20d_pct":    (8.0, -30.0, 30.0),    # close vs 20 sessions ago (includes the last 5)
    "move_5d_pct":      (-3.0, -15.0, 15.0),   # the last 5 sessions: a dip, or a push higher
    "atr_pct":          (2.5, 0.5, 10.0),      # average daily range, % of price
    "volume_ratio":     (1.0, 0.3, 5.0),       # the last session vs the 20-day average
    "equity":           (5000.0, 100.0, 100000.0),
}
PRESETS = {
    "Clean pullback":      {"trend_20d_pct": 10, "move_5d_pct": -4, "atr_pct": 2.2, "volume_ratio": 0.8},
    "Extended breakout":   {"trend_20d_pct": 22, "move_5d_pct": 9, "atr_pct": 3.5, "volume_ratio": 2.4},
    "Earnings next week":  {"trend_20d_pct": 9, "move_5d_pct": -3, "atr_pct": 2.6, "days_to_earnings": 4},
    "Breakdown":           {"trend_20d_pct": -12, "move_5d_pct": -7, "atr_pct": 4.0, "volume_ratio": 2.0},
    "Bear market":         {"trend_20d_pct": 6, "move_5d_pct": -3, "atr_pct": 3.0, "regime": "BEAR"},
}


class ScenarioError(ValueError):
    """The scenario can't be run as given."""


def clean(raw: dict) -> dict:
    """A validated scenario: numbers clamped to LIMITS, text trimmed, nothing else kept."""
    s = {}
    for k, (default, lo, hi) in LIMITS.items():
        try:
            v = float(raw.get(k, default))
        except (TypeError, ValueError):
            v = default
        s[k] = round(min(hi, max(lo, v)), 2)
    ticker = re.sub(r"[^A-Z]", "", str(raw.get("ticker") or "DEMO").upper())[:5] or "DEMO"
    s["ticker"] = ticker
    s["company"] = str(raw.get("company") or f"{ticker} Corp")[:60]
    s["sector"] = str(raw.get("sector") or "Technology")[:40]
    regime = str(raw.get("regime") or "BULL").upper()
    s["regime"] = regime if regime in REGIMES else "BULL"
    dte = raw.get("days_to_earnings")
    try:
        s["days_to_earnings"] = None if dte in (None, "") else int(min(60, max(0, int(float(dte)))))
    except (TypeError, ValueError):
        s["days_to_earnings"] = None
    heads = raw.get("headlines") or []
    if isinstance(heads, str):
        heads = heads.splitlines()
    s["headlines"] = [str(h).strip()[:200] for h in heads if str(h).strip()][:3]
    return s


def _today() -> date:
    return datetime.now(NY).date()


def last_sessions(today: date, n: int) -> list[date]:
    """The last n NYSE sessions up to today (weekdays if the calendar is unavailable)."""
    start = today - timedelta(days=int(n * 1.6) + 10)
    try:
        import pandas_market_calendars as mcal
        days = [d.date() for d in mcal.get_calendar("NYSE").schedule(start_date=start, end_date=today).index]
    except Exception:
        days = [d.date() for d in pd.bdate_range(start, today)]
    return days[-n:]


def make_bars(s: dict, sessions: list) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(ticker bars, SPY bars) on `sessions`, seeded by the scenario so a rerun gives the same
    chart. The close path is shaped in log space, then scaled so the last close is exactly s["price"]."""
    key = repr(sorted((k, str(v)) for k, v in s.items() if k != "headlines"))
    seed = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    n = len(sessions)
    atr = s["atr_pct"] / 100
    noise_sd = atr * 0.6                                                   # daily moves the size of the range
    r20, m5 = np.log1p(s["trend_20d_pct"] / 100), np.log1p(s["move_5d_pct"] / 100)
    drift = np.full(n, r20 / 20 * 0.4)                                     # a gentler trend before
    logp = np.cumsum(drift + np.array([rng.gauss(0, noise_sd) for _ in range(n)]))
    # Bend the last 20 sessions so they match exactly: the 20-day change includes the last 5.
    for start, end, target in ((n - 21, n - 6, r20 - m5), (n - 6, n - 1, m5)):
        err = target - (logp[end] - logp[start])
        steps = end - start
        logp[start + 1:end + 1] += err * np.arange(1, steps + 1) / steps
        logp[end + 1:] += err
    close = np.exp(logp - logp[-1]) * s["price"]
    opn = np.r_[close[0], close[:-1]] * np.exp([rng.gauss(0, noise_sd * 0.4) for _ in range(n)])
    rng_w = close * atr
    high = np.maximum(opn, close) + rng_w * np.array([rng.uniform(0.15, 0.5) for _ in range(n)])
    low = np.minimum(opn, close) - rng_w * np.array([rng.uniform(0.15, 0.5) for _ in range(n)])
    vol = np.array([rng.uniform(0.8, 1.2) for _ in range(n)]) * 2_000_000
    vol[-1] = vol[-21:-1].mean() * s["volume_ratio"]
    dates = pd.to_datetime([str(d) for d in sessions])
    df = pd.DataFrame({"date": dates, "open": opn, "high": high, "low": low, "close": close,
                       "volume": vol.round()}).round({"open": 2, "high": 2, "low": 2, "close": 2})
    spy_close = 500 * np.exp(np.cumsum(np.full(n, SPY_DRIFT[s["regime"]]) +
                                       np.array([rng.gauss(0, 0.006) for _ in range(n)])))
    spy = pd.DataFrame({"date": dates, "close": spy_close.round(2)})
    return df, spy


def build_context(s: dict, df: pd.DataFrame, spy: pd.DataFrame, swing_ind: dict, today: date) -> dict:
    """The same context shape as context_builder.build_context, from the scenario instead of the database."""
    indicators = _compute_indicators(df)
    indicators["rsi_14"] = swing_ind.get("rsi_14")
    anomalies = _detect_anomalies(df, indicators)
    recent = df.tail(10)[["date", "open", "high", "low", "close", "volume"]].copy()
    recent["date"] = recent["date"].dt.strftime("%Y-%m-%d")
    spy_last, spy_ma = float(spy["close"].iloc[-1]), float(spy["close"].tail(120).mean())
    earnings = {}
    if s["days_to_earnings"] is not None:
        d = s["days_to_earnings"]
        earnings = {"days_to_earnings": d, "next_earnings_date": None, "earnings_time": None,
                    "earnings_risk": "HIGH" if d <= 5 else "MEDIUM" if d <= 14 else "LOW",
                    "eps_surprise_last_pct": None, "beat_streak": None}
        idx = last_sessions(today, 1)[-1]
        ahead = pd.bdate_range(pd.Timestamp(idx), periods=d + 1)
        earnings["next_earnings_date"] = ahead[-1].date().isoformat()
    return {
        "ticker": s["ticker"], "as_of_date": today.isoformat(), "trading_mode": "swing",
        "metadata": {"company_name": s["company"], "sector": s["sector"]},
        "indicators": indicators, "anomalies": anomalies, "fundamentals": {},
        "news": [{"published": today.isoformat(), "source": "Scenario", "title": h} for h in s["headlines"]],
        "prior_signal": None, "prior_plan": None, "options": None, "smart_money": None,
        "earnings": earnings, "swing": swing_ind, "price_table": recent.to_string(index=False),
        "num_rows": len(df),
        "market_context": {"regime": {"regime": s["regime"], "risk_floor": RISK_FLOOR[s["regime"]],
                                      "spy_vs_ma200_pct": round((spy_last / spy_ma - 1) * 100, 1),
                                      "vix_close": {"BULL": 15, "CHOPPY": 21, "CORRECTION": 26, "BEAR": 33}[s["regime"]]}},
    }


def run(raw: dict, analyst: Callable[[dict], dict], today: Optional[date] = None) -> dict:
    """The whole path for one scenario. `analyst(ctx)` returns analyst_agent.analyse's dict."""
    from swing_policy import compute_swing_indicators
    s = clean(raw)
    today = today or _today()
    sessions = last_sessions(today, N_BARS)
    df, spy = make_bars(s, sessions)
    swing_ind = compute_swing_indicators(df, spy)
    ctx = build_context(s, df, spy, swing_ind, today)

    a = analyst(ctx)
    if not a:
        raise ScenarioError("Claude's answer couldn't be read as a trade plan. Try again.")

    returns = df["close"].pct_change().dropna().tail(30)
    vol = float(returns.std() * np.sqrt(252))
    risk = score_ticker({"ticker": s["ticker"], "signal": a.get("signal"), "signal_strength": a.get("signal_strength"),
                         "volatility": vol, "anomalies": ctx["anomalies"], "anomaly_count": len(ctx["anomalies"]),
                         "days_to_earnings": s["days_to_earnings"]}, s["regime"], set())
    level = effective_level(None, RISK_FLOOR[s["regime"]])

    order = None
    if a.get("signal") == "BUY" and a.get("entry_price") is not None:
        plan = {"run_id": "playground", "ticker": s["ticker"], "report_date": today,
                "signal_strength": a.get("signal_strength"), "setup_type": a.get("setup_type"),
                "entry_price": a["entry_price"], "stop_price": a["stop_price"], "target_price": a["target_price"],
                "horizon_days": a["horizon_days"], "risk_reward": a.get("risk_reward")}
        account = GuardContext(today=today, equity=s["equity"], buying_power=s["equity"], positions={},
                               bought_today=set(), sold_today=set(), open_orders=[], universe=None,
                               day_trades_5_sessions=0)
        o = build_entries(account, [plan], {s["ticker"]: risk}, level)[0]
        order = {k: o.get(k) for k in ("status", "guard_reasons", "qty", "limit_price", "stop_price", "target_price",
                                       "horizon_days", "risk_dollars", "note")}

    pick = ("latest_close", "trend", "ma_10", "ma_20", "ma_50", "atr_14", "atr_pct", "rsi_14", "ret_5d_pct",
            "ret_20d_pct", "rs_vs_spy_20d_pct", "price_vs_ma20_pct", "volume_ratio", "latest_date")
    return {
        "scenario": s,
        "bars": [{"date": d.strftime("%Y-%m-%d"), "close": c} for d, c in zip(df["date"].tail(60), df["close"].tail(60))],
        "indicators": {k: swing_ind.get(k) for k in pick},
        "anomalies": ctx["anomalies"],
        "analyst": {k: a.get(k) for k in ("signal", "signal_strength", "setup_type", "entry_price", "stop_price",
                                          "target_price", "horizon_days", "risk_reward", "summary", "reasoning",
                                          "invalidation", "model_signal", "swing_flags", "swing_notes", "model",
                                          "prompt_tokens", "output_tokens")},
        "risk": {**risk, "volatility": round(vol, 3)},
        "level": level,
        "order": order,
        "equity": s["equity"],
    }


def claude_analyst(api_key: str) -> Callable[[dict], dict]:
    """The real analyst with the visitor's key: the same prompt, model and rules as the pipeline."""
    from analyst_agent import analyse

    def call(ctx: dict) -> dict:
        return analyse(ctx, api_key=api_key, raise_errors=True)
    return call
