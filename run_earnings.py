"""
run_earnings.py — Orblex Industries Earnings Calendar Agent
──────────────────────────────────────────────────────────
Fetches upcoming earnings dates, EPS estimates, and historical
EPS surprise data via yfinance. Stores a countdown + risk flag
for each ticker so the Analyst Agent and dashboard can surface
"earnings in N days" warnings.

Tables (create via schema below):
  earnings_calendar   — per-ticker upcoming & recent earnings

Usage:
  python run_earnings.py
  python run_earnings.py --tickers AAPL TSLA
  python run_earnings.py --dry-run
"""

import os, sys, json, argparse, uuid, datetime
import yfinance as yf
import pandas as pd
from supabase import create_client
import anthropic

from llm import ask

from dotenv import load_dotenv
load_dotenv()

from config import load_config

SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

# Earnings calendar data (next date, EPS history) is not available via Polygon —
# yfinance is the only viable source here.
_cfg = load_config()
DEFAULT_TICKERS = _cfg.tickers

# Model and per-model request rules: llm.py.

# ── Schema SQL ─────────────────────────────────────────────────────────────────
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS earnings_calendar (
    id                    BIGSERIAL PRIMARY KEY,
    run_id                TEXT NOT NULL,
    ticker                TEXT NOT NULL,
    fetch_date            DATE NOT NULL,
    next_earnings_date    DATE,
    days_to_earnings      INT,
    earnings_time         TEXT,          -- 'BMO' before market open, 'AMC' after close
    eps_estimate          NUMERIC(10,4),
    eps_actual_last       NUMERIC(10,4),
    eps_surprise_last_pct NUMERIC(8,2),  -- % beat/miss last quarter
    revenue_estimate      NUMERIC(20,2),
    revenue_actual_last   NUMERIC(20,2),
    beat_streak           INT,           -- consecutive beats (+ = beats, - = misses)
    earnings_risk         TEXT,          -- HIGH (<=7 days) / MEDIUM (8-21) / LOW (>21)
    historical_surprises  JSONB,         -- last 4 quarters [{date, eps_est, eps_act, surprise_pct}]
    claude_commentary     TEXT,
    prompt_tokens         INT,
    output_tokens         INT,
    created_at            TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(ticker, fetch_date)
);
"""

# ── Helpers ────────────────────────────────────────────────────────────────────

def get_run_id():
    return f"earnings-{datetime.date.today().isoformat()}-{uuid.uuid4().hex[:6]}"

def earnings_risk_level(days: int | None) -> str:
    if days is None:
        return "UNKNOWN"
    if days <= 7:
        return "HIGH"
    if days <= 21:
        return "MEDIUM"
    return "LOW"

def compute_beat_streak(surprises: list) -> int:
    """Positive = beat streak, negative = miss streak."""
    streak = 0
    for s in reversed(surprises):
        pct = s.get("surprise_pct", 0) or 0
        if streak == 0:
            streak = 1 if pct > 0 else -1
        elif streak > 0 and pct > 0:
            streak += 1
        elif streak < 0 and pct <= 0:
            streak -= 1
        else:
            break
    return streak

def claude_earnings_commentary(client: anthropic.Anthropic, ticker: str, data: dict) -> dict:
    prompt = f"""You are a fundamental analyst at Orblex Industries.

Ticker: {ticker}
Next earnings: {data.get('next_earnings_date', 'Unknown')} ({data.get('days_to_earnings', '?')} days away)
EPS estimate: {data.get('eps_estimate')}
Last EPS actual: {data.get('eps_actual_last')}
Last surprise: {data.get('eps_surprise_last_pct')}%
Beat streak: {data.get('beat_streak')} quarters
Historical surprises: {json.dumps(data.get('historical_surprises', []))}

Write a brief 2-sentence earnings risk commentary for this ticker. Note:
- Whether it consistently beats or misses
- The magnitude of upcoming earnings risk
- Any pattern in surprises

Respond ONLY in JSON (no markdown):
{{"commentary": "your 2-sentence commentary here"}}"""

    msg = ask("fast", None, prompt, 400, client)
    raw = msg.text.strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"commentary": raw[:300]}
    result["prompt_tokens"] = msg.input_tokens
    result["output_tokens"] = msg.output_tokens
    return result

# ── Core fetch ─────────────────────────────────────────────────────────────────

def fetch_earnings_for_ticker(ticker: str, run_id: str, client: anthropic.Anthropic) -> dict | None:
    today = datetime.date.today()
    t = yf.Ticker(ticker)

    # ── Next earnings date ──
    next_date = None
    days_to   = None
    timing    = None

    try:
        cal = t.calendar
        if isinstance(cal, dict):
            raw_dates = cal.get("Earnings Date", [])
        elif cal is not None and hasattr(cal, "empty") and not cal.empty and "Earnings Date" in cal.index:
            raw_dates = cal.loc["Earnings Date"].values
        else:
            raw_dates = []
        future = [d for d in raw_dates if pd.notna(d) and pd.Timestamp(d).date() >= today]
        if future:
            next_date = pd.Timestamp(min(future)).date()
            days_to   = (next_date - today).days
    except Exception:
        pass

    # Fallback: earnings_dates from yfinance
    if next_date is None:
        try:
            ed = t.earnings_dates
            if ed is not None and not ed.empty:
                today_ts  = pd.Timestamp(today).tz_localize(ed.index.tz) if ed.index.tz else pd.Timestamp(today)
                future_idx = ed.index[ed.index >= today_ts]
                if len(future_idx) > 0:
                    next_date = future_idx[-1].date()
                    days_to   = (next_date - today).days
        except Exception:
            pass

    # ── EPS estimates & actuals ──
    eps_estimate  = None
    rev_estimate  = None
    eps_actual    = None
    rev_actual    = None
    surprise_pct  = None
    surprises     = []

    try:
        # earnings_history gives past results (attribute name varies by yfinance version)
        hist = getattr(t, "earnings_history", None)
        if hist is None:
            try:
                hist = t.get_earnings_history()
            except Exception:
                pass
        if hist is not None and not hist.empty:
            recent = hist.head(4)
            for _, row in recent.iterrows():
                est = row.get("epsEstimate")
                act = row.get("epsActual")
                surp = ((act - est) / abs(est) * 100) if (est and act and est != 0) else None
                surprises.append({
                    "date": str(row.name.date()) if hasattr(row.name, "date") else str(row.name)[:10],
                    "eps_estimate": float(est) if est else None,
                    "eps_actual": float(act) if act else None,
                    "surprise_pct": round(float(surp), 2) if surp else None,
                })
            if surprises:
                eps_actual   = surprises[0].get("eps_actual")
                surprise_pct = surprises[0].get("surprise_pct")
    except Exception:
        pass

    try:
        info = t.info
        eps_estimate = info.get("forwardEps")
        rev_estimate = info.get("revenueEstimate") or info.get("totalRevenue")
        if not eps_estimate:
            eps_estimate = info.get("trailingEps")
    except Exception:
        pass

    beat_streak = compute_beat_streak(surprises)
    risk_level  = earnings_risk_level(days_to)

    row_data = {
        "next_earnings_date":    next_date.isoformat() if next_date else None,
        "days_to_earnings":      days_to,
        "earnings_time":         timing,
        "eps_estimate":          float(eps_estimate) if eps_estimate else None,
        "eps_actual_last":       float(eps_actual) if eps_actual else None,
        "eps_surprise_last_pct": float(surprise_pct) if surprise_pct else None,
        "revenue_estimate":      float(rev_estimate) if rev_estimate else None,
        "revenue_actual_last":   None,
        "beat_streak":           beat_streak,
        "earnings_risk":         risk_level,
        "historical_surprises":  surprises,
    }

    print(f"  [{ticker}] Next earnings: {next_date} ({days_to}d) | Risk: {risk_level}")

    # Claude commentary
    print(f"  [{ticker}] Generating earnings commentary...")
    commentary = claude_earnings_commentary(client, ticker, row_data)

    return {
        "run_id":               run_id,
        "ticker":               ticker,
        "fetch_date":           today.isoformat(),
        **row_data,
        "historical_surprises": json.dumps(surprises),
        "claude_commentary":    commentary.get("commentary", ""),
        "prompt_tokens":        commentary.get("prompt_tokens", 0),
        "output_tokens":        commentary.get("output_tokens", 0),
    }

# ── Saver ──────────────────────────────────────────────────────────────────────

def save_earnings(sb, row: dict, dry_run: bool):
    if dry_run:
        print(f"  [DRY RUN] Would upsert earnings row for {row['ticker']}")
        return
    sb.table("earnings_calendar").upsert(
        [row], on_conflict="ticker,fetch_date"
    ).execute()

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Orblex Industries — Earnings Calendar Agent")
    parser.add_argument("--tickers", nargs="+")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-id")
    args = parser.parse_args()

    tickers = [t.upper() for t in (args.tickers or DEFAULT_TICKERS)]
    run_id  = args.run_id or get_run_id()
    dry_run = args.dry_run

    print(f"\n{'='*55}")
    print(f"  Orblex Industries — Earnings Calendar Agent")
    print(f"  Run ID : {run_id}")
    print(f"  Tickers: {', '.join(tickers)}")
    print(f"  Dry run: {dry_run}")
    print(f"{'='*55}\n")

    sb     = None if dry_run else create_client(SUPABASE_URL, SUPABASE_KEY)
    claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    for ticker in tickers:
        print(f"[{ticker}] Fetching earnings data...")
        try:
            row = fetch_earnings_for_ticker(ticker, run_id, claude)
            if row:
                save_earnings(sb, row, dry_run)
                if not dry_run:
                    print(f"  [{ticker}] Saved ✓")
        except Exception as e:
            print(f"  [{ticker}] ERROR: {e}")
            import traceback; traceback.print_exc()

    print(f"\n✓ Earnings calendar agent complete. Run ID: {run_id}\n")

if __name__ == "__main__":
    main()