"""
price_checks.py — checks the morning plan against Robinhood's own prices.

The pipeline's history comes from Yahoo (data_provider). Before any order is
proposed, the morning task fetches Robinhood quotes (get_equity_quotes,
read-only) for every ticker in play, and these checks run on them:

  1. Official close — Robinhood returns the official prior-session close
     (close.price / close.date, SIP). The latest close stored in stock_prices
     must be from that same session and within CLOSE_TOLERANCE_PCT of it, or
     the ticker's entry is blocked BAD_PRICE_DATA: its levels were planned
     from a stale or wrong Yahoo bar.
  2. Live price — the most recent of the regular and extended-hours trade.
     At or below the plan stop → QUOTE_BELOW_STOP (trade_guard). A symbol
     that is not active → NOT_TRADING. Otherwise the order's note says where
     the price sits against the limit, e.g. "live 362.10, +1.9% above limit".

Pure functions, no I/O.
"""

import re
from datetime import date, datetime
from typing import Optional

CLOSE_TOLERANCE_PCT = 0.5


def _f(x) -> Optional[float]:
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _ts(x) -> Optional[datetime]:
    # Robinhood sends 1–9 fractional digits (nanoseconds, trailing zeros dropped);
    # cut to microseconds so fromisoformat accepts every length.
    if not x:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", str(x).replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def parse_quotes(*responses) -> dict:
    """
    {SYMBOL: {"live", "live_at", "bid", "ask", "official_close", "close_date",
    "state", "has_traded"}} merged from one or more get_equity_quotes responses
    (the raw response or its "data" object). Later responses win on overlap.
    """
    out: dict = {}
    for raw in responses:
        data = raw.get("data", raw) if isinstance(raw, dict) else {}
        for r in data.get("results", []) or []:
            q, c = r.get("quote") or {}, r.get("close") or {}
            sym = (q.get("symbol") or c.get("symbol") or "").upper()
            if not sym:
                continue
            reg, reg_at = _f(q.get("last_trade_price")), _ts(q.get("venue_last_trade_time"))
            ext, ext_at = _f(q.get("last_non_reg_trade_price")), _ts(q.get("venue_last_non_reg_trade_time"))
            if ext and (not reg or (ext_at and reg_at and ext_at > reg_at) or (ext_at and not reg_at)):
                live, live_at = ext, ext_at
            else:
                live, live_at = reg, reg_at
            close_px = _f(c.get("price"))
            close_date = c.get("date")
            if close_px is None:   # official close unavailable — fall back to the quote's previous close
                close_px, close_date = _f(q.get("previous_close")), q.get("previous_close_date")
            out[sym] = {
                "live": live, "live_at": live_at.isoformat() if live_at else None,
                "bid": _f(q.get("bid_price")), "ask": _f(q.get("ask_price")),
                "official_close": close_px,
                "close_date": str(close_date)[:10] if close_date else None,
                "state": q.get("state"), "has_traded": q.get("has_traded"),
            }
    return out


def close_issue(stored_close: Optional[float], stored_date: Optional[str], quote: Optional[dict]) -> Optional[str]:
    """Why our stored close cannot be trusted against Robinhood's official close, or None."""
    if not quote or quote.get("official_close") is None:
        return "no Robinhood quote to verify the close"
    if stored_close is None or not stored_date:
        return "no stored close"
    if quote.get("close_date") and str(stored_date)[:10] != quote["close_date"]:
        return f"stored close is from {str(stored_date)[:10]}, Robinhood's last session is {quote['close_date']}"
    diff = (stored_close / quote["official_close"] - 1) * 100
    if abs(diff) > CLOSE_TOLERANCE_PCT:
        return (f"stored close {stored_close:.2f} vs official {quote['official_close']:.2f} "
                f"({diff:+.2f}%, tolerance {CLOSE_TOLERANCE_PCT}%)")
    return None


def live_note(order: dict, quote: Optional[dict]) -> Optional[str]:
    """Where the live price sits against an entry's limit and stop."""
    if not quote or not quote.get("live"):
        return None
    live, limit, stop = quote["live"], order.get("limit_price"), order.get("stop_price")
    when = f" @ {quote['live_at'][11:16]}Z" if quote.get("live_at") else ""
    if not limit:
        return f"live {live:.2f}{when}"
    gap = (live / limit - 1) * 100
    if stop and live <= stop:
        where = "at or below the stop"
    elif live <= limit:
        where = f"{gap:+.1f}% vs limit — would fill now"
    else:
        where = f"{gap:+.1f}% above limit — fills only on a pullback"
    return f"live {live:.2f}{when}, {where}"


def entry_reasons(quote: Optional[dict], close_problem: Optional[str]) -> list[str]:
    """Blocking reasons from the Robinhood checks (QUOTE_BELOW_STOP is trade_guard's)."""
    reasons = []
    if close_problem:
        reasons.append("BAD_PRICE_DATA")
    if quote and quote.get("state") and quote["state"] != "active":
        reasons.append("NOT_TRADING")
    return reasons
