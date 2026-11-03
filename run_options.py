"""
run_options.py — Orblex Industries Options Flow Agent
─────────────────────────────────────────────────────
Fetches options chain data via yfinance and stores:
  - Put/Call ratio (volume & OI)
  - Max pain price
  - Key expiry dates (near-term & opex)
  - Top strikes by open interest
  - Unusual options activity (high OI, volume spikes)

Tables (create via schema below):
  options_flow        — per-ticker, per-expiry summary
  options_strikes     — top strike-level detail rows

Usage:
  python run_options.py
  python run_options.py --tickers AAPL TSLA NVDA
  python run_options.py --dry-run
  python run_options.py --run-id custom-run-id
"""

import os, sys, json, argparse, uuid, datetime
from typing import Optional
import pandas as pd
from supabase import create_client
import anthropic

from llm import ask

from dotenv import load_dotenv
load_dotenv()

from config import load_config
from data_provider import get_provider, YFinanceProvider

SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

_cfg = load_config()
DEFAULT_TICKERS = _cfg.tickers

# Model and per-model request rules: llm.py.

# ── Schema SQL (run once in Supabase) ─────────────────────────────────────────
SCHEMA_SQL = """
-- Options flow summary per ticker/expiry
CREATE TABLE IF NOT EXISTS options_flow (
    id               BIGSERIAL PRIMARY KEY,
    run_id           TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    snapshot_date    DATE NOT NULL,
    expiry_date      DATE NOT NULL,
    days_to_expiry   INT,
    call_volume      BIGINT,
    put_volume       BIGINT,
    put_call_ratio_volume NUMERIC(6,3),
    call_oi          BIGINT,
    put_oi           BIGINT,
    put_call_ratio_oi NUMERIC(6,3),
    max_pain_price   NUMERIC(12,4),
    current_price    NUMERIC(12,4),
    iv_rank          NUMERIC(6,2),
    unusual_activity JSONB,
    flow_signal      TEXT,         -- BULLISH / BEARISH / NEUTRAL
    signal_reasoning TEXT,
    prompt_tokens    INT,
    output_tokens    INT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(ticker, snapshot_date, expiry_date)
);

-- Top strikes per expiry
CREATE TABLE IF NOT EXISTS options_strikes (
    id             BIGSERIAL PRIMARY KEY,
    run_id         TEXT NOT NULL,
    ticker         TEXT NOT NULL,
    snapshot_date  DATE NOT NULL,
    expiry_date    DATE NOT NULL,
    strike         NUMERIC(12,4),
    option_type    TEXT,          -- CALL / PUT
    open_interest  BIGINT,
    volume         BIGINT,
    implied_vol    NUMERIC(8,4),
    delta          NUMERIC(8,4),
    in_the_money   BOOLEAN,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);
"""

# ── Helpers ────────────────────────────────────────────────────────────────────

def get_run_id():
    today = datetime.date.today().isoformat()
    return f"options-{today}-{uuid.uuid4().hex[:6]}"

def compute_max_pain(calls_df: pd.DataFrame, puts_df: pd.DataFrame) -> float:
    """
    Max pain = strike price where total option dollar loss to holders is maximised.
    Iterates over all strikes, computes total ITM payout at each, returns the minimum.
    """
    if calls_df.empty or puts_df.empty:
        return 0.0
    all_strikes = sorted(set(calls_df["strike"].tolist() + puts_df["strike"].tolist()))
    pain = {}
    for s in all_strikes:
        # Call holders lose when underlying closes below strike
        call_loss = sum(
            max(s - row["strike"], 0) * row.get("openInterest", 0)
            for _, row in calls_df.iterrows()
        )
        # Put holders lose when underlying closes above strike
        put_loss = sum(
            max(row["strike"] - s, 0) * row.get("openInterest", 0)
            for _, row in puts_df.iterrows()
        )
        pain[s] = call_loss + put_loss
    return float(min(pain, key=pain.get)) if pain else 0.0

def detect_unusual_activity(calls_df: pd.DataFrame, puts_df: pd.DataFrame, threshold: float = 3.0) -> list:
    """Flag strikes where volume >> open_interest (unusual buying)."""
    unusual = []
    for df, otype in [(calls_df, "CALL"), (puts_df, "PUT")]:
        if df.empty:
            continue
        for _, row in df.iterrows():
            oi = row.get("openInterest", 0) or 0
            vol = row.get("volume", 0) or 0
            if oi > 0 and vol / oi >= threshold and vol > 500:
                unusual.append({
                    "type": otype,
                    "strike": float(row["strike"]),
                    "volume": int(vol),
                    "open_interest": int(oi),
                    "ratio": round(vol / oi, 2),
                })
    return sorted(unusual, key=lambda x: -x["ratio"])[:5]

def select_expiries(expiry_strings: list) -> list:
    """Pick: nearest, next monthly opex, and one further out."""
    today = datetime.date.today()
    dated = [datetime.date.fromisoformat(e) for e in expiry_strings
             if datetime.date.fromisoformat(e) > today]
    if not dated:
        return []
    selected = [dated[0]]  # nearest expiry
    # Monthly opex (3rd Friday)
    for d in dated:
        if d.weekday() == 4:
            week_num = (d.day - 1) // 7 + 1
            if week_num == 3 and d not in selected:
                selected.append(d)
                break
    # One further out (30-90 days)
    far = [d for d in dated if 30 <= (d - today).days <= 90 and d not in selected]
    if far:
        selected.append(far[0])
    return selected[:3]

# ── Claude signal ──────────────────────────────────────────────────────────────

def claude_options_signal(client: anthropic.Anthropic, ticker: str, price: float, expiries_data: list) -> dict:
    prompt = f"""You are a quantitative options analyst for Orblex Industries.

Ticker: {ticker}
Current price: ${price:.2f}

Options flow summary across expiries:
{json.dumps(expiries_data, indent=2, default=str)}

Analyse the options flow data. Consider:
1. Put/Call ratio trend (PCR < 0.7 = bullish, PCR > 1.2 = bearish)
2. Max pain vs current price (price gravitates to max pain near expiry)
3. Unusual activity — aggressive buying of calls or puts
4. IV levels (high IV = market expects big move)

Respond ONLY in JSON (no markdown, no preamble):
{{
  "flow_signal": "BULLISH" | "BEARISH" | "NEUTRAL",
  "signal_reasoning": "2–3 sentence explanation citing specific PCR, max pain, unusual activity"
}}"""

    msg = ask("fast", None, prompt, 500, client)
    raw = msg.text.strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"flow_signal": "NEUTRAL", "signal_reasoning": raw[:300]}
    result["prompt_tokens"] = msg.input_tokens
    result["output_tokens"] = msg.output_tokens
    return result

# ── IV rank ──────────────────────────────────────────────────────────────────
# IV rank needs a history of implied vol to rank the current level against. We do
# not store a dedicated IV series, but options_strikes is an insert-only log of
# per-strike implied_vol per snapshot — so current and historical ATM IV are both
# derived from that SAME sampling (top strikes by OI), keeping the rank consistent.
# If there is not enough history to rank honestly, iv_rank stays NULL rather than
# reporting a fabricated number.

IV_RANK_LOOKBACK_DAYS = 365
IV_RANK_MIN_DAYS      = 8      # below this the rank is noise → leave NULL
_ATM_DELTA_LO, _ATM_DELTA_HI = 0.35, 0.65


def _atm_iv_from_strikes(rows: list) -> Optional[float]:
    """
    Mean implied vol of the near-the-money strikes in `rows` (dicts with keys
    'implied_vol' and 'delta'). Prefers |delta| in [0.35, 0.65]; if delta is
    missing/zero across the board, falls back to all positive-IV rows. Returns
    None when no usable IV is present. The SAME rule is applied to the current
    snapshot and to each historical day, so the rank compares like with like.
    """
    atm = [
        r["implied_vol"] for r in rows
        if r.get("implied_vol") and _ATM_DELTA_LO <= abs(r.get("delta") or 0.0) <= _ATM_DELTA_HI
    ]
    if not atm:
        atm = [r["implied_vol"] for r in rows if r.get("implied_vol")]
    return round(sum(atm) / len(atm), 6) if atm else None


def compute_iv_rank(sb, ticker: str, current_iv: Optional[float], today) -> Optional[float]:
    """
    IV rank (0-100) of `current_iv` within the trailing ATM-IV history for the
    ticker, reconstructed from options_strikes. Excludes today's snapshot (not yet
    written) and returns None when history is too thin or flat to rank against.
    """
    if sb is None or current_iv is None:
        return None
    since = (today - datetime.timedelta(days=IV_RANK_LOOKBACK_DAYS)).isoformat()
    try:
        resp = (
            sb.table("options_strikes")
            .select("snapshot_date,implied_vol,delta")
            .eq("ticker", ticker)
            .lt("snapshot_date", today.isoformat())
            .gte("snapshot_date", since)
            .execute()
        )
        rows = resp.data or []
    except Exception as exc:
        print(f"  [{ticker}] IV-rank history lookup failed: {exc}")
        return None

    by_day: dict = {}
    for r in rows:
        by_day.setdefault(r["snapshot_date"], []).append(r)
    history = [v for v in (_atm_iv_from_strikes(day) for day in by_day.values()) if v is not None]

    if len(history) < IV_RANK_MIN_DAYS:
        return None
    lo, hi = min(history), max(history)
    if hi <= lo:
        return None
    rank = (current_iv - lo) / (hi - lo) * 100
    return round(max(0.0, min(100.0, rank)), 2)


# ── Core fetch ─────────────────────────────────────────────────────────────────

def fetch_options_for_ticker(ticker: str, run_id: str, client: anthropic.Anthropic, dry_run: bool = False, sb=None) -> list:
    today    = datetime.date.today()
    provider = get_provider()

    chain_data = provider.fetch_options_chain(ticker)

    # Fall back to yfinance if primary provider returned nothing
    if chain_data is None and provider.name != "yfinance":
        print(f"  [{ticker}] {provider.name} options unavailable — falling back to yfinance")
        chain_data = YFinanceProvider().fetch_options_chain(ticker)

    if chain_data is None:
        print(f"  [{ticker}] No options data available")
        return []

    current_price = chain_data["current_price"]
    expiries      = select_expiries(chain_data["expiries"])

    if not expiries:
        print(f"  [{ticker}] No options data available")
        return []

    flow_rows = []
    strikes_rows = []
    expiries_summary = []

    for exp in expiries:
        exp_str = exp.isoformat()
        if exp_str not in chain_data["chains"]:
            print(f"  [{ticker}] No chain data for expiry {exp_str}")
            continue

        calls = chain_data["chains"][exp_str]["calls"].copy()
        puts  = chain_data["chains"][exp_str]["puts"].copy()

        # Aggregate stats
        call_vol = int(calls["volume"].fillna(0).sum())
        put_vol  = int(puts["volume"].fillna(0).sum())
        call_oi  = int(calls["openInterest"].fillna(0).sum())
        put_oi   = int(puts["openInterest"].fillna(0).sum())

        pcr_vol = round(put_vol / call_vol, 3) if call_vol > 0 else 0.0
        pcr_oi  = round(put_oi  / call_oi,  3) if call_oi  > 0 else 0.0

        max_pain = compute_max_pain(calls, puts)
        unusual  = detect_unusual_activity(calls, puts)
        dte      = (exp - today).days

        expiries_summary.append({
            "expiry": exp_str,
            "dte": dte,
            "pcr_volume": pcr_vol,
            "pcr_oi": pcr_oi,
            "max_pain": max_pain,
            "current_price": current_price,
            "unusual_activity": unusual,
        })

        flow_rows.append({
            "run_id": run_id,
            "ticker": ticker,
            "snapshot_date": today.isoformat(),
            "expiry_date": exp_str,
            "days_to_expiry": dte,
            "call_volume": call_vol,
            "put_volume": put_vol,
            "put_call_ratio_volume": pcr_vol,
            "call_oi": call_oi,
            "put_oi": put_oi,
            "put_call_ratio_oi": pcr_oi,
            "max_pain_price": max_pain,
            "current_price": current_price,
            "unusual_activity": json.dumps(unusual),
            "flow_signal": None,  # filled after Claude call
            "signal_reasoning": None,
            "prompt_tokens": 0,
            "output_tokens": 0,
        })

        # Top-10 strikes by OI
        for df, otype in [(calls, "CALL"), (puts, "PUT")]:
            want_cols = ["strike", "openInterest", "volume", "impliedVolatility", "delta", "inTheMoney"]
            avail_cols = [c for c in want_cols if c in df.columns]
            top = df.nlargest(5, "openInterest")[avail_cols]
            for _, row in top.iterrows():
                strikes_rows.append({
                    "run_id": run_id,
                    "ticker": ticker,
                    "snapshot_date": today.isoformat(),
                    "expiry_date": exp_str,
                    "strike": float(row.get("strike", 0)),
                    "option_type": otype,
                    "open_interest": int(v) if (v := row.get("openInterest", 0)) and v == v else 0,
                    "volume": int(v) if (v := row.get("volume", 0)) and v == v else 0,
                    "implied_vol": float(v) if (v := row.get("impliedVolatility", 0)) and v == v else 0.0,
                    "delta": float(v) if (v := row.get("delta", 0)) and v == v else 0.0,
                    "in_the_money": bool(row.get("inTheMoney", False)),
                })

    if not flow_rows:
        return []

    # IV rank for the ticker: current ATM IV vs its own trailing history, both
    # sampled from options_strikes. Set on every flow row (it is a ticker-level
    # measure, not per-expiry). None when history is too thin — never fabricated.
    current_atm_iv = _atm_iv_from_strikes(strikes_rows)
    iv_rank = compute_iv_rank(sb, ticker, current_atm_iv, today)
    for fr in flow_rows:
        fr["iv_rank"] = iv_rank

    # Claude signal (single call per ticker across all expiries)
    print(f"  [{ticker}] Calling Claude for options signal...")
    signal = claude_options_signal(client, ticker, current_price, expiries_summary)

    # Apply signal to the nearest-expiry row (most relevant)
    flow_rows[0]["flow_signal"] = signal.get("flow_signal", "NEUTRAL")
    flow_rows[0]["signal_reasoning"] = signal.get("signal_reasoning", "")
    flow_rows[0]["prompt_tokens"] = signal.get("prompt_tokens", 0)
    flow_rows[0]["output_tokens"] = signal.get("output_tokens", 0)

    return {"flow": flow_rows, "strikes": strikes_rows}

# ── Saver ──────────────────────────────────────────────────────────────────────

def save_options(supabase_client, data: dict, dry_run: bool):
    if dry_run:
        print("  [DRY RUN] Would save options_flow:", len(data["flow"]), "rows")
        print("  [DRY RUN] Would save options_strikes:", len(data["strikes"]), "rows")
        return

    if data["flow"]:
        supabase_client.table("options_flow").upsert(
            data["flow"],
            on_conflict="ticker,snapshot_date,expiry_date"
        ).execute()

    if data["strikes"]:
        supabase_client.table("options_strikes").insert(data["strikes"]).execute()

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Orblex Industries — Options Flow Agent")
    parser.add_argument("--tickers", nargs="+", help="Override tickers")
    parser.add_argument("--dry-run", action="store_true", help="Skip DB writes")
    parser.add_argument("--run-id", help="Custom run ID")
    args = parser.parse_args()

    tickers = [t.upper() for t in (args.tickers or DEFAULT_TICKERS)]
    run_id  = args.run_id or get_run_id()
    dry_run = args.dry_run

    print(f"\n{'='*55}")
    print(f"  Orblex Industries — Options Flow Agent")
    print(f"  Run ID : {run_id}")
    print(f"  Tickers: {', '.join(tickers)}")
    print(f"  Dry run: {dry_run}")
    print(f"{'='*55}\n")

    if not dry_run:
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    else:
        sb = None

    claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    for ticker in tickers:
        print(f"[{ticker}] Fetching options chain...")
        try:
            result = fetch_options_for_ticker(ticker, run_id, claude, dry_run, sb=sb)
            if result:
                signal = result["flow"][0].get("flow_signal", "N/A")
                print(f"  [{ticker}] Signal: {signal} | Expiries: {len(result['flow'])}")
                if not dry_run:
                    save_options(sb, result, dry_run)
                    print(f"  [{ticker}] Saved to Supabase ✓")
        except Exception as e:
            print(f"  [{ticker}] ERROR: {e}")
            import traceback; traceback.print_exc()

    print(f"\n✓ Options flow agent complete. Run ID: {run_id}\n")

if __name__ == "__main__":
    main()