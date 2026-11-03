"""
swing_policy.py — deterministic rules for TRADING_MODE=swing (1–10 trading day
holds, long only, no day trades).

Same principle as instrument_policy: the model PROPOSES a trade (signal, entry,
stop, target, horizon) and Python DISPOSES. Everything here is rule-based:

  compute_swing_indicators()  short-horizon technicals the swing prompt reads
                              (ATR, MA10/20/50 trend, 20-day range, gap,
                              relative strength vs SPY, Bollinger %B)
  enforce_swing_rules()       validates the model's trade plan after the call:
                              - stale data: no new BUY off an old close
                              - earnings blackout: no new BUY if the report
                                lands inside the holding window
                              - entry/stop/target sanity, ATR-based fallbacks
                              - minimum reward:risk, else BUY → HOLD
                              - horizon clamped to [MIN, MAX]_HOLD_DAYS

Signal semantics in swing mode (long-only account):
  BUY  open a new swing long with a limit at entry_price (good for
       ENTRY_VALID_SESSIONS sessions), with stop and target
  HOLD no new entry; an existing position keeps its stop/target
  SELL the setup has broken; exit an existing position (never a short)
"""

import logging
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

SWING_LOOKBACK_DAYS = 120   # calendar days → ~80 bars, enough for MA50 + ATR14

MIN_HOLD_DAYS = 1           # no day trades: the earliest exit is the next session
MAX_HOLD_DAYS = 10

MIN_REWARD_RISK = 1.5       # target distance must be ≥ 1.5× stop distance
DEFAULT_STOP_ATR = 1.5      # fallback stop when the model's is missing/invalid
MIN_STOP_ATR = 0.5          # tighter than this is noise — one bar can take it out
MAX_STOP_ATR = 4.0          # wider than this is no longer a swing stop
MAX_ENTRY_ABOVE_CLOSE_ATR = 0.5   # don't chase: entry at most 0.5 ATR above last close
MAX_ENTRY_BELOW_CLOSE_ATR = 2.0   # pullback limit entries rest at most 2 ATR below
ENTRY_VALID_SESSIONS = 2          # an unfilled entry limit expires after this many sessions
EARNINGS_BUFFER_DAYS = 1    # be flat at least one session before the report

# Flags written to swing_flags so the dashboard / watchdog can see every correction.
FLAG_STALE_DATA       = "STALE_PRICE_DATA"         # BUY → HOLD, price ingest is behind
FLAG_EARNINGS_BLOCK   = "EARNINGS_BLACKOUT"        # BUY → HOLD, report inside window
FLAG_EARNINGS_CLAMP   = "HORIZON_CLAMPED_EARNINGS" # horizon shortened to exit pre-report
FLAG_LOW_RR           = "LOW_REWARD_RISK"          # BUY → HOLD
FLAG_STOP_DEFAULTED   = "STOP_ATR_DEFAULT"
FLAG_TARGET_DEFAULTED = "TARGET_RR_DEFAULT"
FLAG_ENTRY_CLAMPED    = "ENTRY_CLAMPED"
FLAG_HORIZON_CLAMPED  = "HORIZON_CLAMPED"


# ── Indicators ────────────────────────────────────────────────────────────────

def _r(x, nd=2):
    return None if x is None or pd.isna(x) else round(float(x), nd)


def compute_swing_indicators(df: pd.DataFrame, spy_df: Optional[pd.DataFrame] = None) -> dict:
    """
    Short-horizon technicals from a date-ascending OHLCV frame. Every field is
    None when there is not enough history for it — the prompt renders N/A.
    """
    if df.empty or len(df) < 15:
        return {}

    high, low = df["high"].astype(float), df["low"].astype(float)
    close, opn = df["close"].astype(float), df["open"].astype(float)
    volume = df["volume"].astype(float)
    last, prev = close.iloc[-1], close.iloc[-2]
    n = len(close)

    def ret(k):
        return (last / close.iloc[-1 - k] - 1) * 100 if n > k else None

    def ma(k):
        return close.rolling(k).mean().iloc[-1] if n >= k else None

    # ATR(14), Wilder smoothing.
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]

    # RSI(14), Wilder smoothing (the long-term context uses a simple mean).
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi_s = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    rsi, rsi_3ago = rsi_s.iloc[-1], rsi_s.iloc[-4] if n >= 4 else None

    ma10, ma20, ma50 = ma(10), ma(20), ma(50)
    ma20_5ago = close.rolling(20).mean().iloc[-6] if n >= 25 else None

    if ma20 is not None and ma50 is not None:
        if last > ma20 > ma50:
            trend = "UP"
        elif last < ma20 < ma50:
            trend = "DOWN"
        else:
            trend = "MIXED"
    else:
        trend = None

    # 20-day range excludes today, so "breakout" means above the PRIOR range.
    hi20 = high.iloc[-21:-1].max() if n >= 21 else None
    lo20 = low.iloc[-21:-1].min() if n >= 21 else None

    sd20 = close.rolling(20).std().iloc[-1] if n >= 20 else None
    pct_b = ((last - (ma20 - 2 * sd20)) / (4 * sd20)) if sd20 else None

    bar_range = high.iloc[-1] - low.iloc[-1]
    close_loc = (last - low.iloc[-1]) / bar_range if bar_range > 0 else None

    # Consecutive up (+) / down (−) closes.
    streak = 0
    for d in reversed(delta.dropna().tolist()):
        if d > 0 and streak >= 0:
            streak += 1
        elif d < 0 and streak <= 0:
            streak -= 1
        else:
            break

    avg_vol20 = volume.iloc[:-1].tail(20).mean()

    rs5 = rs20 = None
    if spy_df is not None and not spy_df.empty:
        spy = spy_df.set_index("date")["close"].astype(float)
        aligned = spy.reindex(df["date"]).dropna()
        # Only trust RS when SPY covers the same sessions (SPY has backfilled gaps).
        if len(aligned) >= 6 and aligned.index[-1] == df["date"].iloc[-1]:
            s_last = aligned.iloc[-1]
            r5 = ret(5)
            if r5 is not None:
                rs5 = r5 - (s_last / aligned.iloc[-6] - 1) * 100
            if len(aligned) >= 21 and ret(20) is not None:
                rs20 = ret(20) - (s_last / aligned.iloc[-21] - 1) * 100

    return {
        "latest_close":      _r(last, 4),
        "prev_close":        _r(prev, 4),
        "latest_date":       df["date"].iloc[-1].strftime("%Y-%m-%d"),
        "day_change_pct":    _r((last / prev - 1) * 100),
        "gap_pct":           _r((opn.iloc[-1] / prev - 1) * 100),
        "ret_5d_pct":        _r(ret(5)),
        "ret_10d_pct":       _r(ret(10)),
        "ret_20d_pct":       _r(ret(20)),
        "atr_14":            _r(atr, 4),
        "atr_pct":           _r(atr / last * 100),
        "ma_10":             _r(ma10, 4),
        "ma_20":             _r(ma20, 4),
        "ma_50":             _r(ma50, 4),
        "price_vs_ma20_pct": _r((last / ma20 - 1) * 100) if ma20 else None,
        "price_vs_ma50_pct": _r((last / ma50 - 1) * 100) if ma50 else None,
        "ma20_slope_5d_pct": _r((ma20 / ma20_5ago - 1) * 100) if ma20 and ma20_5ago else None,
        "trend":             trend,
        "rsi_14":            _r(rsi, 1),
        "rsi_14_3d_ago":     _r(rsi_3ago, 1),
        "high_20d":          _r(hi20, 4),
        "low_20d":           _r(lo20, 4),
        "dist_high_20d_atr": _r((hi20 - last) / atr) if hi20 and atr else None,
        "dist_low_20d_atr":  _r((last - lo20) / atr) if lo20 and atr else None,
        "bollinger_pct_b":   _r(pct_b),
        "close_location":    _r(close_loc),
        "streak_days":       streak,
        "latest_volume":     int(volume.iloc[-1]),
        "avg_volume_20d":    int(avg_vol20) if avg_vol20 == avg_vol20 else None,
        "volume_ratio":      _r(volume.iloc[-1] / avg_vol20) if avg_vol20 else None,
        "rs_vs_spy_5d_pct":  _r(rs5),
        "rs_vs_spy_20d_pct": _r(rs20),
    }


def sessions_missing(latest_bar_iso: Optional[str], today: Optional[date] = None) -> Optional[int]:
    """
    NYSE sessions strictly between the latest stored bar and today. 0 means the
    data is current for a pre-open run (the bar is the previous session's close);
    anything higher means the price ingest has not run and levels are stale.
    """
    if not latest_bar_iso:
        return None
    today = today or date.today()
    latest = date.fromisoformat(str(latest_bar_iso)[:10])
    if latest >= today:
        return 0
    try:
        import pandas_market_calendars as mcal
        sched = mcal.get_calendar("NYSE").schedule(start_date=latest, end_date=today)
        days = [d.date() for d in sched.index]
    except Exception:
        days = [d.date() for d in pd.bdate_range(latest, today)]
    return sum(1 for d in days if latest < d < today)


def trading_days_until(target_iso: Optional[str], today: Optional[date] = None) -> Optional[int]:
    """NYSE-ish weekday count from today (exclusive) to target (exclusive of today,
    inclusive of the target day). Holidays are ignored — that errs toward a longer
    count by a day at most, and the earnings buffer absorbs it."""
    if not target_iso:
        return None
    today = today or date.today()
    try:
        target = date.fromisoformat(str(target_iso)[:10])
    except ValueError:
        return None
    if target < today:
        return None
    return int(np.busday_count(today, target)) + (1 if np.is_busday(target) else 0)


# ── Post-model enforcement ────────────────────────────────────────────────────

def _num(x) -> Optional[float]:
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def enforce_swing_rules(analysis: dict, ind: dict, earnings: dict,
                        today: Optional[date] = None) -> dict:
    """
    Validate and correct the model's swing trade plan in place; returns it.
    Every correction appends a flag to analysis["swing_flags"] and a note to
    analysis["swing_notes"]. Levels are only kept on BUY (SELL/HOLD carry no new
    entry), but the model's levels are logged for a BUY that gets downgraded.
    """
    flags: list[str] = []
    notes: list[str] = []
    signal = analysis.get("signal")
    close = ind.get("latest_close")
    atr = ind.get("atr_14")

    # Horizon
    try:
        horizon = int(analysis.get("horizon_days") or 5)
    except (TypeError, ValueError):
        horizon = 5
    clamped = max(MIN_HOLD_DAYS, min(MAX_HOLD_DAYS, horizon))
    if clamped != horizon:
        flags.append(FLAG_HORIZON_CLAMPED)
        notes.append(f"horizon {horizon}d clamped to {clamped}d")
    horizon = clamped

    # Stale prices: levels set off an old close are meaningless for an order today.
    missing = sessions_missing(ind.get("latest_date"), today)
    if signal == "BUY" and missing:
        flags.append(FLAG_STALE_DATA)
        notes.append(f"latest bar {ind.get('latest_date')} is {missing} session(s) old: no new entry")
        signal = "HOLD"

    # Earnings blackout — computed from the report date, not the stored
    # days_to_earnings, which is as of the earnings stage's fetch.
    tdays = trading_days_until((earnings or {}).get("next_earnings_date"), today)
    analysis["trading_days_to_earnings"] = tdays
    if signal == "BUY" and tdays is not None:
        last_safe = tdays - 1 - EARNINGS_BUFFER_DAYS   # sessions we can hold and still exit before the report
        if last_safe < MIN_HOLD_DAYS:
            flags.append(FLAG_EARNINGS_BLOCK)
            notes.append(f"earnings in {tdays} trading day(s): no new entry")
            signal = "HOLD"
        elif horizon > last_safe:
            flags.append(FLAG_EARNINGS_CLAMP)
            notes.append(f"horizon {horizon}d → {last_safe}d to exit before earnings in {tdays}d")
            horizon = last_safe

    entry = _num(analysis.get("entry_price"))
    stop = _num(analysis.get("stop_price"))
    target = _num(analysis.get("target_price"))

    if signal == "BUY" and close and atr:
        lo, hi = close - MAX_ENTRY_BELOW_CLOSE_ATR * atr, close + MAX_ENTRY_ABOVE_CLOSE_ATR * atr
        if entry is None or not (lo <= entry <= hi):
            new_entry = round(min(max(entry or close, lo), hi), 2)
            flags.append(FLAG_ENTRY_CLAMPED)
            notes.append(f"entry {entry} → {new_entry}")
            entry = new_entry

        if stop is None or stop >= entry or not (MIN_STOP_ATR * atr <= entry - stop <= MAX_STOP_ATR * atr):
            new_stop = round(entry - DEFAULT_STOP_ATR * atr, 2)
            flags.append(FLAG_STOP_DEFAULTED)
            notes.append(f"stop {stop} → {new_stop} ({DEFAULT_STOP_ATR} ATR)")
            stop = new_stop

        risk = entry - stop
        if target is None or target <= entry:
            new_target = round(entry + 2 * risk, 2)
            flags.append(FLAG_TARGET_DEFAULTED)
            notes.append(f"target {target} → {new_target} (2R)")
            target = new_target

        rr = (target - entry) / risk
        analysis["risk_reward"] = round(rr, 2)
        if rr < MIN_REWARD_RISK:
            flags.append(FLAG_LOW_RR)
            notes.append(f"reward:risk {rr:.2f} < {MIN_REWARD_RISK}: no entry")
            signal = "HOLD"
    else:
        analysis["risk_reward"] = None

    if signal != analysis.get("signal"):
        notes.append(f"signal {analysis['signal']} → {signal}")
        analysis["model_signal"] = analysis["signal"]
        analysis["signal"] = signal
        if signal == "HOLD" and analysis.get("signal_strength") == "STRONG":
            analysis["signal_strength"] = "MODERATE"

    if analysis["signal"] == "BUY":
        analysis.update(entry_price=entry, stop_price=stop, target_price=target, horizon_days=horizon)
    else:
        analysis.update(entry_price=None, stop_price=None, target_price=None,
                        horizon_days=None, risk_reward=None)

    analysis["swing_flags"] = flags
    analysis["swing_notes"] = notes
    if notes:
        logger.info(f"  Swing rules: {'; '.join(notes)}")
    return analysis
