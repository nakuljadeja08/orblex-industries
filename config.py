"""
config.py — loads and validates all environment variables
"""

import os
from pathlib import Path
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

UNIVERSE_FILE = Path(__file__).with_name("universe.txt")

# Last-resort fallback if universe.txt is missing and TICKERS is unset.
DEFAULT_TICKERS = "NBIS,AAPL,MU,NVDA,GOOG,QCOM"

TRADING_MODES = ("position", "swing")


def _read_universe_file(path: Path = UNIVERSE_FILE) -> list[str]:
    """
    Parse the committed ticker universe.

    One symbol per line; blank lines ignored, and everything from a '#' to
    end of line is a comment. Returns [] when the file is absent so the
    caller can fall back.
    """
    if not path.exists():
        return []

    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        sym = line.split("#", 1)[0].strip().upper()
        if sym:
            out.append(sym)
    return out


@dataclass
class Config:
    # Core
    supabase_url:     str
    supabase_key:     str
    tickers:          list[str]
    historical_days:  int
    # Data provider
    data_provider:    str
    polygon_api_key:  str
    # News — NewsAPI (replaces WSJ scraper)
    news_enabled:     bool
    news_provider:    str
    newsapi_key:      str
    # Scheduler
    scheduler_dry_run:  bool
    scheduler_run_time: str
    # Analyst horizon: "position" (original, weeks–months) or "swing" (1–10 day holds)
    trading_mode:     str = "position"


def load_config() -> Config:
    url         = os.getenv("SUPABASE_URL", "").strip()
    key         = os.getenv("SUPABASE_KEY", "").strip()
    # Precedence: TICKERS env var (explicit one-off override) > universe.txt
    # (the committed source of truth) > built-in fallback.
    tickers_raw = os.getenv("TICKERS", "").strip()
    if tickers_raw:
        tickers_src = "TICKERS env var"
    else:
        _file_tickers = _read_universe_file()
        if _file_tickers:
            tickers_raw, tickers_src = ",".join(_file_tickers), str(UNIVERSE_FILE.name)
        else:
            tickers_raw, tickers_src = DEFAULT_TICKERS, "built-in default"
    hist_days   = int(os.getenv("HISTORICAL_DAYS", "90"))

    data_provider   = os.getenv("DATA_PROVIDER", "yfinance").strip().lower()
    polygon_api_key = os.getenv("POLYGON_API_KEY", "").strip()

    news_enabled  = os.getenv("NEWS_ENABLED", "false").strip().lower() == "true"
    news_provider = os.getenv("NEWS_PROVIDER", "newsapi").strip().lower()
    newsapi_key   = os.getenv("NEWSAPI_KEY", "").strip()

    scheduler_dry_run  = os.getenv("SCHEDULER_DRY_RUN", "false").lower() == "true"
    scheduler_run_time = os.getenv("SCHEDULER_RUN_TIME", "08:30").strip()

    trading_mode = os.getenv("TRADING_MODE", "position").strip().lower()

    # ── Validation ────────────────────────────────────────────────────────────
    missing = []
    if not url or url.startswith("https://your-project"):
        missing.append("SUPABASE_URL")
    if not key or key == "your-service-role-key-here":
        missing.append("SUPABASE_KEY")

    if missing:
        raise EnvironmentError(
            f"Missing or placeholder values in .env: {', '.join(missing)}\n"
            "Copy .env.example → .env and fill in your Supabase credentials."
        )

    if trading_mode not in TRADING_MODES:
        raise EnvironmentError(
            f"TRADING_MODE={trading_mode!r} is not one of {', '.join(TRADING_MODES)}."
        )

    if data_provider == "polygon" and not polygon_api_key:
        raise EnvironmentError(
            "DATA_PROVIDER=polygon requires POLYGON_API_KEY in .env."
        )

    # NewsAPI key is optional — falls back to RSS gracefully
    if news_enabled and news_provider == "newsapi" and not newsapi_key:
        import logging
        logging.getLogger(__name__).warning(
            "NEWS_ENABLED=true but NEWSAPI_KEY not set — falling back to WSJ RSS."
        )

    tickers = list(dict.fromkeys(
        t.strip().upper() for t in tickers_raw.split(",") if t.strip()))

    if not tickers:
        raise EnvironmentError(
            f"No tickers resolved from {tickers_src}. Populate universe.txt "
            f"or set TICKERS in the environment."
        )

    import logging
    logging.getLogger(__name__).info(
        "Universe: %d tickers from %s", len(tickers), tickers_src
    )

    return Config(
        supabase_url=url,
        supabase_key=key,
        tickers=tickers,
        historical_days=hist_days,
        data_provider=data_provider,
        polygon_api_key=polygon_api_key,
        news_enabled=news_enabled,
        news_provider=news_provider,
        newsapi_key=newsapi_key,
        scheduler_dry_run=scheduler_dry_run,
        scheduler_run_time=scheduler_run_time,
        trading_mode=trading_mode,
    )