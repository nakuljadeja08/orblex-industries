"""
data_provider.py — Orblex Industries Data Provider Abstraction Layer

Defines a common DataProvider interface so the pipeline can swap between
yfinance (free, dev) and Polygon.io (production) without touching any
agent or pipeline code.

Usage in fetcher.py (or pipeline.py):
    from data_provider import get_provider
    provider = get_provider()               # reads DATA_PROVIDER from .env
    df       = provider.fetch_prices("AAPL", days=90)
    meta     = provider.fetch_metadata("AAPL")

Set in .env:
    DATA_PROVIDER=yfinance        # default — free, no key needed
    DATA_PROVIDER=polygon         # production — requires POLYGON_API_KEY
    POLYGON_API_KEY=your_key_here
"""

import logging
import os
from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


# ── Abstract Interface ────────────────────────────────────────────────────────

class DataProvider(ABC):
    """
    Common interface all data providers must implement.
    Returning empty DataFrame (not raising) is the contract for soft failures.
    """

    @abstractmethod
    def fetch_prices(self, ticker: str, days: int = 90) -> pd.DataFrame:
        """
        Returns DataFrame with columns:
            ticker, date, open, high, low, close, adj_close, volume
        Returns empty DataFrame on failure.
        """
        ...

    @abstractmethod
    def fetch_metadata(self, ticker: str) -> Optional[dict]:
        """
        Returns dict with keys:
            ticker, company_name, sector, industry, market_cap,
            currency, exchange, website, description
        Returns None on failure.
        """
        ...

    @abstractmethod
    def fetch_options_chain(self, ticker: str) -> "dict | None":
        """
        Returns:
          {
            "current_price": float,
            "expiries": [str, ...],          # sorted future expiry date strings YYYY-MM-DD
            "chains": {
              "YYYY-MM-DD": {
                "calls": pd.DataFrame,       # strike, openInterest, volume,
                "puts":  pd.DataFrame,       # impliedVolatility, delta, inTheMoney
              }
            }
          }
        Returns None on failure.
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name for logging."""
        ...


# ── yfinance Provider (existing behaviour, unchanged) ────────────────────────

class YFinanceProvider(DataProvider):
    """
    Original yfinance-backed provider. Zero-config — no API key required.
    Use for development, backtesting, and when Polygon quota is exhausted.
    """

    @property
    def name(self) -> str:
        return "yfinance"

    def fetch_prices(self, ticker: str, days: int = 90) -> pd.DataFrame:
        import yfinance as yf

        start = (date.today() - timedelta(days=days)).isoformat()
        end   = date.today().isoformat()
        logger.info(f"  [{self.name}] Fetching prices: {ticker}  ({start} -> {end})")

        try:
            raw = yf.download(
                ticker,
                start=start,
                end=end,
                auto_adjust=False,
                progress=False,
            )
            if raw.empty:
                logger.warning(f"  [{self.name}] No price data for {ticker}")
                return pd.DataFrame()

            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = [col[0] for col in raw.columns]

            raw = raw.reset_index()
            raw.columns = [c.strip().lower().replace(" ", "_") for c in raw.columns]
            raw.rename(columns={"adjclose": "adj_close", "adj_close": "adj_close"}, inplace=True)

            df = pd.DataFrame({
                "ticker":    ticker,
                "date":      pd.to_datetime(raw["date"]).dt.date,
                "open":      raw["open"].round(4),
                "high":      raw["high"].round(4),
                "low":       raw["low"].round(4),
                "close":     raw["close"].round(4),
                "adj_close": raw.get("adj_close", raw["close"]).round(4),
                "volume":    raw["volume"].astype("Int64"),
            })
            df = df.dropna(subset=["close"])
            logger.info(f"  [{self.name}] OK — {ticker}: {len(df)} rows")
            return df

        except Exception as exc:
            logger.error(f"  [{self.name}] Failed to fetch prices for {ticker}: {exc}")
            return pd.DataFrame()

    def fetch_metadata(self, ticker: str) -> Optional[dict]:
        import yfinance as yf

        logger.info(f"  [{self.name}] Fetching metadata: {ticker}")
        try:
            info = yf.Ticker(ticker).info
            return {
                "ticker":       ticker,
                "company_name": info.get("longName") or info.get("shortName"),
                "sector":       info.get("sector"),
                "industry":     info.get("industry"),
                "market_cap":   info.get("marketCap"),
                "currency":     info.get("currency"),
                "exchange":     info.get("exchange"),
                "website":      info.get("website"),
                "description":  (info.get("longBusinessSummary") or "")[:1000],
            }
        except Exception as exc:
            logger.error(f"  [{self.name}] Failed to fetch metadata for {ticker}: {exc}")
            return None

    def fetch_options_chain(self, ticker: str) -> "dict | None":
        import yfinance as yf

        _COLS = ["strike", "openInterest", "volume", "impliedVolatility", "delta", "inTheMoney"]
        logger.info(f"  [{self.name}] Fetching options chain: {ticker}")
        try:
            t = yf.Ticker(ticker)
            try:
                info = t.info
                current_price = float(info.get("regularMarketPrice") or info.get("currentPrice") or 0.0)
            except Exception:
                current_price = 0.0

            all_expiries = t.options or ()
            future = [e for e in all_expiries if date.fromisoformat(e) > date.today()]
            if not future:
                logger.warning(f"  [{self.name}] No options expiries for {ticker}")
                return None

            chains = {}
            for exp in future:
                try:
                    chain = t.option_chain(exp)
                    def _trim(df):
                        cols = [c for c in _COLS if c in df.columns]
                        return df[cols].copy()
                    chains[exp] = {"calls": _trim(chain.calls), "puts": _trim(chain.puts)}
                except Exception as exc:
                    logger.warning(f"  [{self.name}] Chain fetch failed {ticker}/{exp}: {exc}")

            if not chains:
                return None

            return {"current_price": current_price, "expiries": future, "chains": chains}
        except Exception as exc:
            logger.error(f"  [{self.name}] Options chain failed for {ticker}: {exc}")
            return None


# ── Polygon.io Provider ───────────────────────────────────────────────────────

class PolygonProvider(DataProvider):
    """
    Polygon.io REST API v2 provider.

    Free tier: EOD aggregates (delayed), no options data.
    Paid tier: real-time, full tick history, fundamentals.

    Required .env:
        POLYGON_API_KEY=your_key_here

    Docs: https://polygon.io/docs/stocks/getting-started
    """

    BASE_URL = "https://api.polygon.io"

    def __init__(self):
        self._api_key = os.getenv("POLYGON_API_KEY", "").strip()
        if not self._api_key:
            raise EnvironmentError(
                "POLYGON_API_KEY is not set in .env. "
                "Get a free key at https://polygon.io"
            )

    @property
    def name(self) -> str:
        return "polygon"

    def _get(self, path: str, params: dict = None) -> dict:
        """Thin wrapper around requests.get with auth + error handling."""
        import requests

        params = params or {}
        params["apiKey"] = self._api_key
        url = f"{self.BASE_URL}{path}"

        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _get_all(self, path: str, params: dict = None, max_pages: int = 4) -> list:
        """GET with automatic next_url pagination, returns concatenated results list."""
        import requests

        results = []
        url     = f"{self.BASE_URL}{path}"
        params  = {**(params or {}), "apiKey": self._api_key}

        for _ in range(max_pages):
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("results", []))
            next_url = data.get("next_url")
            if not next_url:
                break
            url    = next_url                    # full URL from Polygon
            params = {"apiKey": self._api_key}   # apiKey only; rest is encoded in next_url

        return results

    def fetch_prices(self, ticker: str, days: int = 90) -> pd.DataFrame:
        """
        Uses Polygon /v2/aggs/ticker/{ticker}/range/1/day/{from}/{to}
        Returns adjusted OHLCV data (vw = volume-weighted average price).
        """
        start = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
        end   = date.today().strftime("%Y-%m-%d")
        logger.info(f"  [{self.name}] Fetching prices: {ticker}  ({start} -> {end})")

        try:
            data = self._get(
                f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
                params={
                    "adjusted": "true",
                    "sort":     "asc",
                    "limit":    "50000",
                },
            )

            results = data.get("results", [])
            if not results:
                logger.warning(f"  [{self.name}] No price data for {ticker}")
                return pd.DataFrame()

            rows = []
            for r in results:
                # Polygon timestamps are Unix ms
                day = date.fromtimestamp(r["t"] / 1000)
                rows.append({
                    "ticker":    ticker,
                    "date":      day,
                    "open":      round(r["o"], 4),
                    "high":      round(r["h"], 4),
                    "low":       round(r["l"], 4),
                    "close":     round(r["c"], 4),
                    "adj_close": round(r["c"], 4),   # already adjusted when adjusted=true
                    "volume":    int(r["v"]),
                })

            df = pd.DataFrame(rows)
            df["volume"] = df["volume"].astype("Int64")
            logger.info(f"  [{self.name}] OK — {ticker}: {len(df)} rows")
            return df

        except Exception as exc:
            logger.error(f"  [{self.name}] Failed to fetch prices for {ticker}: {exc}")
            return pd.DataFrame()

    def fetch_metadata(self, ticker: str) -> Optional[dict]:
        """
        Uses Polygon /v3/reference/tickers/{ticker}
        Returns company details available on free tier.
        """
        logger.info(f"  [{self.name}] Fetching metadata: {ticker}")
        try:
            data    = self._get(f"/v3/reference/tickers/{ticker}")
            results = data.get("results", {})

            if not results:
                logger.warning(f"  [{self.name}] No metadata for {ticker}")
                return None

            return {
                "ticker":       ticker,
                "company_name": results.get("name"),
                "sector":       results.get("sic_description"),    # closest equivalent
                "industry":     results.get("sic_description"),
                "market_cap":   results.get("market_cap"),
                "currency":     results.get("currency_name"),
                "exchange":     results.get("primary_exchange"),
                "website":      results.get("homepage_url"),
                "description":  (results.get("description") or "")[:1000],
            }

        except Exception as exc:
            logger.error(f"  [{self.name}] Failed to fetch metadata for {ticker}: {exc}")
            return None

    def fetch_options_chain(self, ticker: str) -> "dict | None":
        """
        Fetches options chain via Polygon /v3/snapshot/options/{ticker}.
        Requires Options add-on (Starter plan or above).
        Returns None on 403/empty so caller can fall back to yfinance.
        """
        _COLS = ["strike", "openInterest", "volume", "impliedVolatility", "delta", "inTheMoney"]
        today = date.today()
        logger.info(f"  [{self.name}] Fetching options chain: {ticker}")

        def _to_df(contracts: list, contract_type: str, price: float) -> pd.DataFrame:
            rows = []
            for c in contracts:
                d      = c.get("details") or {}
                greeks = c.get("greeks")  or {}
                day    = c.get("day")     or {}
                if d.get("contract_type", "").lower() != contract_type:
                    continue
                strike = float(d.get("strike_price") or 0)
                rows.append({
                    "strike":            strike,
                    "openInterest":      int(c.get("open_interest") or 0),
                    "volume":            int(day.get("volume") or 0),
                    "impliedVolatility": float(c.get("implied_volatility") or 0),
                    "delta":             float(greeks.get("delta") or 0),
                    "inTheMoney":        (strike < price) if contract_type == "call"
                                         else (strike > price),
                })
            return pd.DataFrame(rows) if rows else pd.DataFrame(columns=_COLS)

        try:
            results = self._get_all(
                f"/v3/snapshot/options/{ticker}",
                params={"limit": 250, "sort": "expiration_date", "order": "asc"},
            )
            if not results:
                logger.warning(f"  [{self.name}] No options data for {ticker} — Options add-on required?")
                return None

            ua            = (results[0].get("underlying_asset") or {})
            current_price = float(ua.get("price") or ua.get("close") or 0.0)

            # Group contracts by expiry, future only
            by_expiry: dict = {}
            for r in results:
                exp = (r.get("details") or {}).get("expiration_date")
                if exp and exp > today.isoformat():
                    by_expiry.setdefault(exp, []).append(r)

            if not by_expiry:
                return None

            chains = {
                exp: {
                    "calls": _to_df(contracts, "call", current_price),
                    "puts":  _to_df(contracts, "put",  current_price),
                }
                for exp, contracts in by_expiry.items()
            }

            return {
                "current_price": current_price,
                "expiries":      sorted(by_expiry.keys()),
                "chains":        chains,
            }

        except Exception as exc:
            logger.error(f"  [{self.name}] Options chain failed for {ticker}: {exc}")
            return None


# ── Factory ───────────────────────────────────────────────────────────────────

_PROVIDERS = {
    "yfinance": YFinanceProvider,
    "polygon":  PolygonProvider,
}


def get_provider(override: str = None) -> DataProvider:
    """
    Returns the configured DataProvider instance.

    Priority:
      1. override argument (useful for tests)
      2. DATA_PROVIDER env var
      3. "yfinance" default

    Example:
        provider = get_provider()          # reads .env
        provider = get_provider("polygon") # explicit override
    """
    name = (override or os.getenv("DATA_PROVIDER", "yfinance")).strip().lower()

    if name not in _PROVIDERS:
        raise ValueError(
            f"Unknown DATA_PROVIDER '{name}'. "
            f"Valid options: {list(_PROVIDERS.keys())}"
        )

    logger.info(f"  DataProvider: {name}")
    return _PROVIDERS[name]()