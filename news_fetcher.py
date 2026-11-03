"""
news_fetcher.py — Orblex Industries News Context Fetcher

Fetches recent financial news headlines per ticker and formats them
for injection into the Analyst Agent's prompt as sentiment context.

Provider:
  NewsAPI (newsapi.org) — free tier, 100 req/day

Add to .env:
  NEWS_ENABLED=true
  NEWSAPI_KEY=your_key_here
  NEWS_PROVIDER=newsapi
  MAX_ARTICLES_PER_TICKER=5

Usage (standalone test):
  python news_fetcher.py AAPL NVDA MDB
"""

import logging
import os
import time
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

NEWS_ENABLED            = os.getenv("NEWS_ENABLED", "true").strip().lower() == "true"
NEWS_PROVIDER           = os.getenv("NEWS_PROVIDER", "newsapi").strip().lower()
NEWSAPI_KEY             = os.getenv("NEWSAPI_KEY", "").strip()
MAX_ARTICLES_PER_TICKER = int(os.getenv("MAX_ARTICLES_PER_TICKER", "5"))
REQUEST_DELAY_SEC       = 0.5

# Company name overrides — improves search quality for tickers with
# names that differ significantly from their symbol
COMPANY_NAME_HINTS = {
    "AAPL":  "Apple",
    "NVDA":  "Nvidia",
    "GOOGL": "Alphabet",
    "GOOG":  "Alphabet",
    "MDB":   "MongoDB",
    "MU":    "Micron",
    "QCOM":  "Qualcomm",
    "MSFT":  "Microsoft",
    "AMZN":  "Amazon",
    "META":  "Meta",
    "TSLA":  "Tesla",
    "NBIS":  "Nebius",
    "DRAM":  "Roundhill Memory",
}


# ── NewsAPI Provider ──────────────────────────────────────────────────────────

def _fetch_newsapi(ticker: str, company_name: str) -> list[dict]:
    """
    Fetches news from newsapi.org.

    Search strategy:
      - Primary term: short company name (e.g. "Apple" not "Apple Inc.")
        Most financial articles use the short name, not the ticker symbol
      - Secondary: ticker symbol as fallback in OR
      - No domain filter — casting wide net across all NewsAPI sources
      - 7-day lookback for recency
    """
    if not NEWSAPI_KEY:
        raise EnvironmentError(
            "NEWSAPI_KEY not set in .env. "
            "Get a free key at https://newsapi.org/register"
        )

    # Use hint if available, otherwise first word of company_name
    short_name = COMPANY_NAME_HINTS.get(ticker) or company_name.split(" ")[0]

    # Build query: company short name OR ticker, scoped to finance/stock context
    # Adding "stock" or "earnings" biases toward financial articles
    query   = f'"{short_name}" AND (stock OR earnings OR shares OR market OR revenue)'
    from_dt = (date.today() - timedelta(days=7)).isoformat()

    params = {
        "q":        query,
        "from":     from_dt,
        "sortBy":   "publishedAt",
        "language": "en",
        "pageSize": MAX_ARTICLES_PER_TICKER,
        "apiKey":   NEWSAPI_KEY,
        # No domains filter — searches all 80k+ sources
        # This is what was blocking AAPL and MDB results
    }

    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "ok":
            logger.warning(f"  [news/newsapi] API error for {ticker}: {data.get('message')}")
            return []

    except Exception as exc:
        logger.error(f"  [news/newsapi] Request failed for {ticker}: {exc}")
        return []

    articles = []
    for a in data.get("articles", []):
        headline = (a.get("title") or "").strip()
        # Skip removed/deleted articles that NewsAPI returns as placeholders
        if not headline or headline == "[Removed]":
            continue
        articles.append({
            "headline":       headline,
            "summary":        (a.get("description") or "")[:250].strip(),
            "source":         a.get("source", {}).get("name", ""),
            "url":            a.get("url", ""),
            "published_date": (a.get("publishedAt") or "")[:10],
        })

    return articles


# ── WSJ RSS Fallback ──────────────────────────────────────────────────────────

WSJ_RSS_FEEDS = [
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",
]


def _fetch_wsj_rss(ticker: str, company_name: str) -> list[dict]:
    """WSJ public RSS feed fallback — no auth required."""
    import xml.etree.ElementTree as ET

    short_name   = COMPANY_NAME_HINTS.get(ticker) or company_name.split(" ")[0]
    search_terms = {ticker.lower(), short_name.lower()}
    articles     = []

    for feed_url in WSJ_RSS_FEEDS:
        try:
            resp = requests.get(
                feed_url,
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0 (compatible; OrblexIndustries/1.0)"},
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.content)

            for item in root.findall(".//item"):
                title       = (item.findtext("title") or "").strip()
                description = (item.findtext("description") or "").strip()
                pub_date    = (item.findtext("pubDate") or "")[:16]
                link        = (item.findtext("link") or "").strip()

                combined = (title + " " + description).lower()
                if any(term in combined for term in search_terms):
                    articles.append({
                        "headline":       title,
                        "summary":        description[:250],
                        "source":         "WSJ",
                        "url":            link,
                        "published_date": pub_date,
                    })

            if len(articles) >= MAX_ARTICLES_PER_TICKER:
                break

        except Exception as exc:
            logger.warning(f"  [news/rss] Feed {feed_url} failed: {exc}")
            continue

    return articles[:MAX_ARTICLES_PER_TICKER]


# ── Formatter ─────────────────────────────────────────────────────────────────

def _format_news_block(ticker: str, company_name: str, articles: list[dict]) -> str:
    if not articles:
        return f"No recent news found for {ticker}."

    lines = [f"Recent News — {ticker} ({company_name}):"]
    for i, a in enumerate(articles, 1):
        source = f"[{a['source']}]" if a.get("source") else ""
        date_s = f" ({a['published_date']})" if a.get("published_date") else ""
        lines.append(f"  {i}. {source}{date_s} {a['headline']}")
        if a.get("summary"):
            lines.append(f"     {a['summary']}")

    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_news_context(
    tickers: list[str],
    company_names: dict[str, str] = None,
) -> dict[str, str]:
    """
    Fetches recent news for each ticker.
    Returns dict[ticker -> formatted news block string].
    Never raises — all failures return empty string.
    """
    company_names = company_names or {}

    if not NEWS_ENABLED or NEWS_PROVIDER == "disabled":
        logger.info("  [news] Disabled — skipping")
        return {t: "" for t in tickers}

    if NEWS_PROVIDER == "newsapi" and not NEWSAPI_KEY:
        logger.warning(
            "  [news] NEWSAPI_KEY not set — falling back to WSJ RSS. "
            "Get a free key at https://newsapi.org/register"
        )
        effective_provider = "rss"
    else:
        effective_provider = NEWS_PROVIDER

    results = {}

    for ticker in tickers:
        cname = company_names.get(ticker, ticker)
        logger.info(f"  [news/{effective_provider}] {ticker} ({cname})")

        try:
            articles = (
                _fetch_newsapi(ticker, cname)
                if effective_provider == "newsapi"
                else _fetch_wsj_rss(ticker, cname)
            )
            results[ticker] = _format_news_block(ticker, cname, articles)
            logger.info(f"  [news] {ticker}: {len(articles)} article(s)")

        except Exception as exc:
            logger.error(f"  [news] Error for {ticker}: {exc}")
            results[ticker] = ""

        time.sleep(REQUEST_DELAY_SEC)

    return results


# ── Standalone runner ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")

    tickers = [t.upper() for t in sys.argv[1:]] if len(sys.argv) > 1 else ["AAPL", "NVDA", "MDB"]
    print(f"\nFetching news for: {', '.join(tickers)}\n{'='*60}")

    news = fetch_news_context(tickers)
    for ticker, block in news.items():
        print(f"\n{block}\n{'─'*60}")