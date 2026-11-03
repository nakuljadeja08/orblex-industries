"""
context_builder.py — fetches stock data from Supabase and formats it
into a structured context block for the analyst agent.

v2 (Phase 4): adds latest valuation fundamentals (stock_fundamentals table,
populated by run_fundamentals.py) and optional recent news headlines
(NewsAPI — used only if NEWSAPI_KEY is set in the environment; degrades
gracefully to an empty list otherwise).

v3: adds smart-money positioning — famous investors' 13F holdings and recent
Form 4 / 13D / 13G filings on the ticker (run_smart_money.py).

v4: mode="swing" (TRADING_MODE=swing) builds a short-horizon context instead:
120 days of bars for swing_policy's indicators (ATR, MA10/20/50, 20-day range,
relative strength vs SPY), the last 3 days of news by recency, and only fresh
Form 4 insider filings. Valuation, 13F holdings, and congressional PTRs are
dropped — they are weeks to quarters stale and say nothing about a 1–10 day hold.
"""

import logging
import os
import re
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import requests
from supabase import Client

from swing_policy import SWING_LOOKBACK_DAYS, compute_swing_indicators

logger = logging.getLogger(__name__)

LOOKBACK_DAYS   = 30   # how many days of prices to send to the agent
NEWS_DAYS       = 7    # how far back to pull headlines
NEWS_MAX        = 5    # max headlines per ticker
SWING_NEWS_DAYS  = 3   # swing mode: only headlines a 1–10 day trade can still react to
SWING_FORM4_DAYS = 14  # swing mode: insider filings older than this are priced in
# Tickers that are ordinary English words; news searches use the company name only.
WORD_TICKERS = {"NOW", "PATH", "ON", "ALL", "ARE", "CAN", "FOR", "HAS", "IT", "ONE", "OPEN",
                "PLAY", "REAL", "RUN", "SNOW", "TEAM", "TRUE", "WELL", "ANY", "BIG", "CAR", "LOW"}


def _query_prices(client: Client, ticker: str, days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    since = (date.today() - timedelta(days=days)).isoformat()
    resp = (
        client.table("stock_prices")
        .select("date,open,high,low,close,adj_close,volume")
        .eq("ticker", ticker)
        .gte("date", since)
        .order("date", desc=False)
        .execute()
    )
    if not resp.data:
        return pd.DataFrame()
    df = pd.DataFrame(resp.data)
    df["date"] = pd.to_datetime(df["date"])
    return df


def _query_metadata(client: Client, ticker: str) -> dict:
    resp = (
        client.table("stock_metadata")
        .select("company_name,sector,industry,market_cap")
        .eq("ticker", ticker)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else {}


def _query_prior_signal(client: Client, ticker: str, lookback: int = 60) -> Optional[dict]:
    """
    The analyst's own most recent prior decision on this ticker, from the
    append-only signal_history log (written by run_drift AFTER each analyst run,
    so at analyst time the latest rows are genuinely yesterday's call and earlier
    — never today's). Returns prior signal/strength plus how long that signal has
    been held, or None if there is no prior history (first-ever run for a ticker).

    days_held is calendar days since the signal last changed; held_sessions counts
    the consecutive prior sessions on the same signal. Both let the analyst see
    that, e.g., it has flip-flopped BUY→SELL→BUY over three sessions.
    """
    try:
        resp = (
            client.table("signal_history")
            .select("signal,signal_strength,report_date")
            .eq("ticker", ticker)
            .order("report_date", desc=True)
            .limit(lookback)
            .execute()
        )
    except Exception as e:
        logger.warning(f"  Prior-signal lookup failed for {ticker}: {e}")
        return None

    rows = resp.data or []
    if not rows:
        return None

    # Collapse to one decision per date (keep the first seen — rows are date-desc,
    # so that is the latest run on that date), then drop today's if drift already
    # ran for a same-day re-run: we want the decision(s) made BEFORE now.
    today = date.today().isoformat()
    seen: set[str] = set()
    daily: list[dict] = []
    for r in rows:
        d = r["report_date"]
        if d in seen or d >= today:
            continue
        seen.add(d)
        daily.append(r)

    if not daily:
        return None

    prior = daily[0]
    prior_signal = prior["signal"]

    # Walk back while the signal is unchanged to measure how long it has held.
    streak_start = prior["report_date"]
    held_sessions = 0
    for r in daily:
        if r["signal"] == prior_signal:
            streak_start = r["report_date"]
            held_sessions += 1
        else:
            break

    days_held = (date.today() - date.fromisoformat(streak_start)).days

    return {
        "prior_signal":   prior_signal,
        "prior_strength": prior.get("signal_strength"),
        "prior_date":     prior["report_date"],
        "held_sessions":  held_sessions,
        "days_held":      days_held,
    }


def _query_prior_swing_plan(client: Client, ticker: str) -> Optional[dict]:
    """
    The last swing BUY plan on this ticker (entry/stop/target/horizon) from
    agent_reports, so the analyst can manage a live trade against its own levels
    instead of re-deciding from scratch. None if there is none, or if the swing
    columns have not been migrated yet (swing_schema.sql).
    """
    try:
        resp = (
            client.table("agent_reports")
            .select("report_date,entry_price,stop_price,target_price,horizon_days,setup_type")
            .eq("ticker", ticker)
            .eq("trading_mode", "swing")
            .eq("signal", "BUY")
            .lt("report_date", date.today().isoformat())
            .order("report_date", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.debug(f"  Prior swing plan lookup failed for {ticker}: {e}")
        return None
    return resp.data[0] if resp.data else None


def _query_fundamentals(client: Client, ticker: str) -> dict:
    """
    Latest valuation snapshot for the ticker from stock_fundamentals
    (populated by run_fundamentals.py). Empty dict if none exists —
    ETFs and brand-new tickers may legitimately have nothing.
    """
    try:
        resp = (
            client.table("stock_fundamentals")
            .select("trailing_pe,forward_pe,peg_ratio,price_to_sales,price_to_book,"
                    "dividend_yield,profit_margin,revenue_growth,earnings_growth,"
                    "beta,fifty_two_wk_high,fifty_two_wk_low,target_mean_price,"
                    "recommendation_key,snapshot_date")
            .eq("ticker", ticker)
            .order("snapshot_date", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else {}
    except Exception as e:
        logger.warning(f"  Fundamentals lookup failed for {ticker}: {e}")
        return {}


def _query_earnings(client: Client, ticker: str) -> dict:
    """
    Latest earnings_calendar row for the ticker (Stage 0). Empty dict if none.
    Days-to-earnings is the key field: a name reporting in a few days carries
    binary event risk that ordinary technicals do not show.
    """
    try:
        resp = (
            client.table("earnings_calendar")
            .select("next_earnings_date,days_to_earnings,earnings_time,earnings_risk,"
                    "eps_surprise_last_pct,beat_streak,claude_commentary")
            .eq("ticker", ticker)
            .order("fetch_date", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else {}
    except Exception as e:
        logger.warning(f"  Earnings lookup failed for {ticker}: {e}")
        return {}


def _query_options_flow(client: Client, ticker: str) -> dict:
    """
    Nearest-expiry options_flow row from the latest snapshot for the ticker
    (Stage 0). The nearest expiry is the most decision-relevant and is the row
    that carries the Claude flow_signal. Empty dict if no options data (common
    for ETFs and thin names).
    """
    try:
        # Latest snapshot_date for this ticker, then its nearest expiry.
        resp = (
            client.table("options_flow")
            .select("snapshot_date,expiry_date,days_to_expiry,put_call_ratio_volume,"
                    "put_call_ratio_oi,iv_rank,max_pain_price,current_price,flow_signal")
            .eq("ticker", ticker)
            .order("snapshot_date", desc=True)
            .order("days_to_expiry", desc=False)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else {}
    except Exception as e:
        logger.warning(f"  Options-flow lookup failed for {ticker}: {e}")
        return {}


# Funds often hold a different share class than the one the universe tracks
# (Berkshire and Tiger hold GOOGL; the universe tracks GOOG). Same economics.
SHARE_CLASS_ALIASES = {"GOOG": ["GOOG", "GOOGL"], "GOOGL": ["GOOG", "GOOGL"],
                       "BRK-A": ["BRK-A", "BRK-B"], "BRK-B": ["BRK-A", "BRK-B"]}
SMART_MONEY_FILING_DAYS = 90


def _query_smart_money(client: Client, ticker: str, mode: str = "position") -> dict:
    """
    Which tracked famous investors hold this ticker per their latest 13F (with the
    quarter-over-quarter change), plus their Form 4 / 13D / 13G filings on it in
    the last 90 days (run_smart_money.py). Empty dict when nobody holds or traded
    it — the common case, and not a signal in itself.

    Swing mode keeps only Form 4s from the last SWING_FORM4_DAYS: 13F books are a
    quarter stale and PTRs up to 45 days late, too slow for a 1–10 day hold.
    """
    symbols = SHARE_CLASS_ALIASES.get(ticker, [ticker])
    if mode == "swing":
        since = (date.today() - timedelta(days=SWING_FORM4_DAYS)).isoformat()
        try:
            f = (
                client.table("investor_filings")
                .select("manager,form,filing_date,direction,summary")
                .in_("ticker", symbols)
                .in_("form", ["4", "4/A"])
                .gte("filing_date", since)
                .order("filing_date", desc=True)
                .limit(5)
                .execute()
            )
        except Exception as e:
            logger.warning(f"  Smart-money lookup failed for {ticker}: {e}")
            return {}
        return {"holdings": [], "filings": f.data} if f.data else {}
    try:
        h = (
            client.table("investor_holdings")
            .select("manager,fund,period_of_report,put_call,pct_portfolio,"
                    "change_type,share_change_pct,rank")
            .in_("ticker", symbols)
            .execute()
        )
        since = (date.today() - timedelta(days=SMART_MONEY_FILING_DAYS)).isoformat()
        f = (
            client.table("investor_filings")
            .select("manager,form,filing_date,direction,summary")
            .in_("ticker", symbols)
            .gte("filing_date", since)
            .order("filing_date", desc=True)
            .limit(8)
            .execute()
        )
    except Exception as e:
        logger.warning(f"  Smart-money lookup failed for {ticker}: {e}")
        return {}

    holdings = sorted(h.data or [], key=lambda r: -(r.get("pct_portfolio") or 0))
    filings  = f.data or []
    return {"holdings": holdings, "filings": filings} if holdings or filings else {}


def build_market_context(client: Client) -> dict:
    """
    Portfolio-wide Stage 0 context that is identical for every ticker (market
    regime + macro snapshot), so it is fetched ONCE per run rather than per
    ticker. Returned dict is injected into each ticker's context by run_agent.
    Both halves degrade to {} independently.
    """
    regime, macro = {}, {}
    try:
        r = (
            client.table("market_regimes")
            .select("regime,risk_floor,spy_vs_ma200_pct,spy_drawdown_pct,vix_close,"
                    "rationale,regime_date")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        regime = r.data[0] if r.data else {}
    except Exception as e:
        logger.warning(f"  Regime lookup failed: {e}")
    try:
        m = (
            client.table("macro_snapshots")
            .select("macro_regime,cpi_yoy,ppi_yoy,equity_impact,regime_summary,snapshot_date")
            .order("snapshot_date", desc=True)
            .limit(1)
            .execute()
        )
        macro = m.data[0] if m.data else {}
    except Exception as e:
        logger.warning(f"  Macro lookup failed: {e}")
    return {"regime": regime, "macro": macro}


def _fetch_news(ticker: str, company_name: Optional[str] = None,
                days: int = NEWS_DAYS, sort_by: str = "relevancy") -> list[dict]:
    """
    Recent headlines via NewsAPI. Requires NEWSAPI_KEY in the environment;
    returns [] if unset or on any failure. Headlines feed the analyst's
    numeric sentiment_score — they are context, not a hard dependency.
    """
    api_key = os.environ.get("NEWSAPI_KEY")
    if not api_key:
        return []

    # A ticker that is also a common word ("OR NOW", "OR AI") matches nearly
    # every article, so those search by company name only.
    if company_name and (len(ticker) <= 2 or ticker.upper() in WORD_TICKERS):
        short = re.sub(r"[,\s]+(Inc|Incorporated|Corp|Corporation|Co|Ltd|plc|N\.V|Holdings|Group)\.?$",
                       "", company_name.strip(), flags=re.I)
        query = f'"{short or company_name}"'
    else:
        query = f'"{company_name}" OR {ticker}' if company_name else ticker
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q":        query,
                "from":     (date.today() - timedelta(days=days)).isoformat(),
                "language": "en",
                "sortBy":   sort_by,
                "pageSize": NEWS_MAX,
                "apiKey":   api_key,
            },
            timeout=10,
        )
        resp.raise_for_status()
        articles = resp.json().get("articles", [])[:NEWS_MAX]
        return [
            {
                "title":       a.get("title", ""),
                "source":      (a.get("source") or {}).get("name", ""),
                "published":   (a.get("publishedAt") or "")[:10],
            }
            for a in articles if a.get("title")
        ]
    except Exception as e:
        logger.warning(f"  News fetch failed for {ticker}: {e}")
        return []


def _compute_indicators(df: pd.DataFrame) -> dict:
    """Compute simple technical indicators from the price DataFrame."""
    if df.empty or len(df) < 2:
        return {}

    close = df["close"].astype(float)
    volume = df["volume"].astype(float)

    # Returns
    daily_returns = close.pct_change().dropna()
    ret_5d  = (close.iloc[-1] / close.iloc[-6]  - 1) * 100 if len(close) >= 6  else None
    ret_20d = (close.iloc[-1] / close.iloc[-21] - 1) * 100 if len(close) >= 21 else None

    # Moving averages
    ma_5  = close.rolling(5).mean().iloc[-1]  if len(close) >= 5  else None
    ma_20 = close.rolling(20).mean().iloc[-1] if len(close) >= 20 else None

    # Volatility (annualised std of daily returns)
    volatility = daily_returns.std() * (252 ** 0.5) * 100 if len(daily_returns) >= 5 else None

    # Volume anomaly: today's volume vs 20-day average
    avg_vol_20 = volume.iloc[:-1].tail(20).mean()
    latest_vol = volume.iloc[-1]
    vol_ratio  = latest_vol / avg_vol_20 if avg_vol_20 > 0 else None

    # RSI (14-period)
    rsi = None
    if len(close) >= 15:
        delta  = close.diff()
        gain   = delta.clip(lower=0).rolling(14).mean()
        loss   = (-delta.clip(upper=0)).rolling(14).mean()
        rs     = gain / loss.replace(0, float("nan"))
        rsi_s  = 100 - (100 / (1 + rs))
        rsi    = round(float(rsi_s.iloc[-1]), 1) if not rsi_s.iloc[-1] != rsi_s.iloc[-1] else None

    # Price vs MAs
    price_vs_ma5  = ((close.iloc[-1] / ma_5  - 1) * 100) if ma_5  else None
    price_vs_ma20 = ((close.iloc[-1] / ma_20 - 1) * 100) if ma_20 else None

    return {
        "latest_close":   round(float(close.iloc[-1]), 4),
        "prev_close":     round(float(close.iloc[-2]), 4),
        "day_change_pct": round(float(daily_returns.iloc[-1] * 100), 2),
        "ret_5d_pct":     round(ret_5d,  2) if ret_5d  is not None else None,
        "ret_20d_pct":    round(ret_20d, 2) if ret_20d is not None else None,
        "ma_5":           round(float(ma_5),  4) if ma_5  is not None else None,
        "ma_20":          round(float(ma_20), 4) if ma_20 is not None else None,
        "price_vs_ma5_pct":  round(price_vs_ma5,  2) if price_vs_ma5  is not None else None,
        "price_vs_ma20_pct": round(price_vs_ma20, 2) if price_vs_ma20 is not None else None,
        "rsi_14":         rsi,
        "volatility_ann_pct": round(volatility, 2) if volatility is not None else None,
        "latest_volume":  int(latest_vol),
        "avg_volume_20d": int(avg_vol_20),
        "volume_ratio":   round(vol_ratio, 2) if vol_ratio is not None else None,
    }


def _detect_anomalies(df: pd.DataFrame, indicators: dict) -> list[dict]:
    """Rule-based anomaly flags — passed to the agent as additional context."""
    anomalies = []

    vol_ratio = indicators.get("volume_ratio")
    if vol_ratio and vol_ratio >= 2.0:
        anomalies.append({
            "type":    "volume_spike",
            "detail":  f"Volume {vol_ratio:.1f}x the 20-day average",
            "severity": "high" if vol_ratio >= 3.0 else "medium",
        })

    day_chg = indicators.get("day_change_pct")
    if day_chg is not None and abs(day_chg) >= 3.0:
        anomalies.append({
            "type":    "large_price_move",
            "detail":  f"Day change of {day_chg:+.2f}%",
            "severity": "high" if abs(day_chg) >= 5.0 else "medium",
        })

    rsi = indicators.get("rsi_14")
    if rsi is not None:
        if rsi >= 70:
            anomalies.append({"type": "overbought", "detail": f"RSI at {rsi}", "severity": "medium"})
        elif rsi <= 30:
            anomalies.append({"type": "oversold",   "detail": f"RSI at {rsi}", "severity": "medium"})

    # MA crossover signal
    ma5, ma20 = indicators.get("ma_5"), indicators.get("ma_20")
    if ma5 and ma20:
        if len(df) >= 2:
            prev_close = float(df["close"].astype(float).iloc[-2])
            prev_ma5   = float(df["close"].astype(float).rolling(5).mean().iloc[-2])
            curr_close = indicators["latest_close"]
            if prev_close < prev_ma5 and curr_close > float(ma5):
                anomalies.append({"type": "ma_crossover_bullish", "detail": "Price crossed above MA5", "severity": "low"})
            elif prev_close > prev_ma5 and curr_close < float(ma5):
                anomalies.append({"type": "ma_crossover_bearish", "detail": "Price crossed below MA5", "severity": "low"})

    return anomalies


def build_context(client: Client, ticker: str, mode: str = "position",
                  spy_df: Optional[pd.DataFrame] = None) -> Optional[dict]:
    """
    Main entry point. Returns a dict with all data needed by the agent,
    or None if there's not enough data for this ticker.

    mode="swing" adds ctx["swing"] (swing_policy indicators; spy_df is the SPY
    frame for relative strength, fetched once per run via query_spy_prices) and
    ctx["prior_plan"], and drops the long-horizon inputs (see module docstring).
    """
    logger.info(f"  Building context for {ticker} ({mode})")
    swing = mode == "swing"

    df   = _query_prices(client, ticker, days=SWING_LOOKBACK_DAYS if swing else LOOKBACK_DAYS)
    meta = _query_metadata(client, ticker)

    min_rows = 15 if swing else 5
    if df.empty or len(df) < min_rows:
        logger.warning(f"  Insufficient price data for {ticker} (need ≥{min_rows} rows)")
        return None

    indicators   = _compute_indicators(df)
    swing_ind    = compute_swing_indicators(df, spy_df) if swing else None
    if swing:
        # One RSI in the prompt: the anomaly flags must agree with the swing block.
        indicators["rsi_14"] = swing_ind.get("rsi_14")
    anomalies    = _detect_anomalies(df, indicators)
    fundamentals = {} if swing else _query_fundamentals(client, ticker)
    news         = (_fetch_news(ticker, meta.get("company_name"), days=SWING_NEWS_DAYS, sort_by="publishedAt")
                    if swing else _fetch_news(ticker, meta.get("company_name")))
    prior_signal = _query_prior_signal(client, ticker)
    earnings     = _query_earnings(client, ticker)
    options      = _query_options_flow(client, ticker)
    smart_money  = _query_smart_money(client, ticker, mode)

    # Recent price table (last 10 days) for the agent prompt
    recent = df.tail(10)[["date", "open", "high", "low", "close", "volume"]].copy()
    recent["date"] = recent["date"].dt.strftime("%Y-%m-%d")
    price_table = recent.to_string(index=False)

    return {
        "ticker":       ticker,
        "as_of_date":   date.today().isoformat(),
        "metadata":     meta,
        "indicators":   indicators,
        "anomalies":    anomalies,
        "fundamentals": fundamentals,
        "news":         news,
        "prior_signal": prior_signal,
        "earnings":     earnings,
        "options":      options,
        "smart_money":  smart_money,
        "price_table":  price_table,
        "num_rows":     len(df),
        "trading_mode": mode,
        "swing":        swing_ind,
        "prior_plan":   _query_prior_swing_plan(client, ticker) if swing else None,
    }


def query_spy_prices(client: Client) -> pd.DataFrame:
    """
    SPY bars for swing relative strength — fetched once per run, not per ticker.
    SPY is not in the ingested universe (stock_prices only holds the backtest's
    backfill, which lags), so pull it live and fall back to the stored rows.
    """
    try:
        from fetcher import fetch_prices
        df = fetch_prices("SPY", days=SWING_LOOKBACK_DAYS)
        if not df.empty:
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
            return df
    except Exception as e:
        logger.warning(f"  Live SPY fetch failed, using stored rows: {e}")
    return _query_prices(client, "SPY", days=SWING_LOOKBACK_DAYS)