"""
fetcher.py — pulls OHLCV price data and company metadata via the configured DataProvider.

The actual fetching logic lives in data_provider.py. This module is the
stable interface the rest of the pipeline calls — swapping yfinance → Polygon
is a one-line .env change (DATA_PROVIDER=polygon), nothing here changes.
"""

import logging
from typing import Optional

import pandas as pd

from data_provider import get_provider

logger = logging.getLogger(__name__)

# Module-level provider instance — instantiated once per process
_provider = get_provider()


def fetch_prices(ticker: str, days: int = 90) -> pd.DataFrame:
    """
    Fetch daily OHLCV data for a ticker going back `days` calendar days.
    Returns a clean DataFrame ready for upsert, or empty DataFrame on failure.
    """
    return _provider.fetch_prices(ticker, days=days)


def fetch_metadata(ticker: str) -> Optional[dict]:
    """
    Fetch static company info for a ticker.
    Returns a dict or None on failure.
    """
    return _provider.fetch_metadata(ticker)