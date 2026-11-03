"""
universe_selector.py — picks the day's swing universe from the account balance.

Pure functions; run_universe.py does the I/O. In swing mode the analyst only
spends a Claude call on a ticker the account could actually trade:

  HELD     any open position — always analysed (its exits depend on it)
  CORE     universe.txt names the balance can size at least one share of
  SLEEVE   the top UNIVERSE_SLEEVE_SIZE names from candidates.txt that pass
           the screen below, ranked by 20-day relative strength vs SPY, at
           most SLEEVE_MAX_PER_SECTOR from any one sector, and only as many as
           fit under MAX_TICKERS after held and core names
  skipped  everything else, each with the reason (e.g. "1 share needs a
           $1,240 account")

"Sizeable" uses trade_plan's own sizing at a normal risk level and MODERATE
strength: floor(equity × 1% × 0.75 / (1.5 × ATR)) shares, capped by the 30%
position limit. A HIGH-risk day halves that later, in the trade plan; the
universe is chosen on the normal level so it does not churn with the regime.

Sleeve screen (candidates only — the core list is the user's choice):
  price ≥ MIN_PRICE, 20-day average dollar volume ≥ MIN_DOLLAR_VOL,
  ATR ≤ MAX_ATR_PCT of price, sizeable, close above the 50-day average and
  trend not DOWN.
With no account balance on record, the full core list is analysed and no
sleeve is added — the stage then behaves exactly as before.
"""

import math
from typing import Optional

from trade_guard import MAX_POSITION_PCT, RISK_PER_TRADE_PCT
from trade_plan import STRENGTH_RISK

SLEEVE_SIZE = 8
SLEEVE_MAX_PER_SECTOR = 3    # keep the sleeve from piling into one sector
MAX_TICKERS = 30             # cost ceiling: the sleeve only fills room the core leaves
MIN_PRICE = 5.0
MIN_DOLLAR_VOL = 20e6
MAX_ATR_PCT = 6.0
STOP_ATR = 1.5           # the swing default stop distance (swing_policy.DEFAULT_STOP_ATR)
BASE_STRENGTH = "MODERATE"


def max_shares(price: float, atr: float, equity: float) -> int:
    """Whole shares the account could buy under trade_plan's sizing at a normal level."""
    if not price or not atr or equity <= 0:
        return 0
    budget = equity * RISK_PER_TRADE_PCT / 100 * STRENGTH_RISK[BASE_STRENGTH]
    by_risk = math.floor(budget / (STOP_ATR * atr))
    by_cap = math.floor(equity * MAX_POSITION_PCT / 100 / price)
    return max(0, min(by_risk, by_cap))


def equity_for_one_share(price: float, atr: float) -> float:
    """Smallest account that can size one share of this name."""
    risk_rate = RISK_PER_TRADE_PCT / 100 * STRENGTH_RISK[BASE_STRENGTH]
    return max(STOP_ATR * atr / risk_rate, price / (MAX_POSITION_PCT / 100))


def _screen_fail(m: dict, equity: float) -> Optional[str]:
    if m.get("price") is None or m.get("atr") is None:
        return "no price data"
    if m["price"] < MIN_PRICE:
        return f"price ${m['price']:.2f} < ${MIN_PRICE:.0f}"
    if (m.get("dollar_vol") or 0) < MIN_DOLLAR_VOL:
        return f"thin: ${(m.get('dollar_vol') or 0) / 1e6:.0f}M/day traded"
    if m["atr_pct"] > MAX_ATR_PCT:
        return f"too volatile: ATR {m['atr_pct']:.1f}%"
    if max_shares(m["price"], m["atr"], equity) < 1:
        return f"1 share needs a ${equity_for_one_share(m['price'], m['atr']):,.0f} account"
    if m.get("trend") == "DOWN" or (m.get("price_vs_ma50_pct") is not None and m["price_vs_ma50_pct"] <= 0):
        return "not in an uptrend"
    return None


def select_universe(core: list[str], candidates: dict[str, str], held: set[str],
                    metrics: dict[str, dict], equity: Optional[float],
                    sleeve_size: int = SLEEVE_SIZE,
                    max_per_sector: int = SLEEVE_MAX_PER_SECTOR,
                    max_tickers: int = MAX_TICKERS) -> list[dict]:
    """
    One row per ticker considered: {ticker, source, included, reason, rank?,
    price, atr_pct, dollar_vol_m, max_qty, rs20, sector}. Included rows are the
    day's analysis list.
    """
    rows: list[dict] = []

    def row(t, source, included, reason, rank=None):
        m = metrics.get(t) or {}
        qty = max_shares(m.get("price"), m.get("atr"), equity) if equity and m.get("price") else None
        rows.append({
            "ticker": t, "source": source, "included": included, "reason": reason, "rank": rank,
            "price": m.get("price"), "atr_pct": m.get("atr_pct"),
            "dollar_vol_m": round(m["dollar_vol"] / 1e6, 1) if m.get("dollar_vol") else None,
            "max_qty": qty, "rs20": m.get("rs20"), "sector": candidates.get(t),
        })

    core_set = list(dict.fromkeys(t.upper() for t in core))
    held = {t.upper() for t in held}

    if equity is None:
        for t in core_set:
            row(t, "CORE", True, "no account balance on record — full universe")
        for t in sorted(held - set(core_set)):
            row(t, "HELD", True, "open position")
        return rows

    for t in core_set:
        m = metrics.get(t)
        if t in held:
            row(t, "HELD", True, "open position")
        elif not m or m.get("price") is None or m.get("atr") is None:
            row(t, "CORE", True, "no price data — kept")
        else:
            qty = max_shares(m["price"], m["atr"], equity)
            if qty >= 1:
                row(t, "CORE", True, f"sizeable: up to {qty} sh")
            else:
                row(t, "CORE", False, f"1 share needs a ${equity_for_one_share(m['price'], m['atr']):,.0f} account")

    for t in sorted(held - set(core_set)):
        row(t, "HELD", True, "open position")

    passing, failing = [], []
    for t in candidates:
        if t in core_set or t in held:
            continue
        m = metrics.get(t) or {}
        why = _screen_fail(m, equity)
        (failing if why else passing).append((t, why))

    passing.sort(key=lambda x: (-(metrics[x[0]].get("rs20") if metrics[x[0]].get("rs20") is not None else -1e9),
                                -(metrics[x[0]].get("dollar_vol") or 0)))
    # Held positions and sizeable core names come first; the sleeve gets what is
    # left under max_tickers, so the daily Claude spend stays bounded as the
    # account grows (held positions are never dropped, even past the cap).
    sleeve_size = max(0, min(sleeve_size, max_tickers - sum(r["included"] for r in rows)))
    taken, per_sector = 0, {}
    for i, (t, _) in enumerate(passing, start=1):
        rs = metrics[t].get("rs20")
        rs_s = f"RS vs SPY {rs:+.1f} pts" if rs is not None else "RS n/a"
        sec = candidates.get(t)
        if taken >= sleeve_size:
            row(t, "SLEEVE", False, f"ranked #{i} ({rs_s}); sleeve holds {sleeve_size}", rank=i)
        elif per_sector.get(sec, 0) >= max_per_sector:
            row(t, "SLEEVE", False, f"ranked #{i} ({rs_s}); {sec} already has {max_per_sector} sleeve picks", rank=i)
        else:
            taken += 1
            per_sector[sec] = per_sector.get(sec, 0) + 1
            row(t, "SLEEVE", True, f"sleeve #{taken}: {rs_s}", rank=i)
    for t, why in failing:
        row(t, "SLEEVE", False, why)
    return rows
