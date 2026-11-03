"""
run_macro.py — Orblex Industries Macro Indicators Agent
───────────────────────────────────────────────────────
Fetches CPI, PPI, and PCE data from the BLS public API (no key
required for basic access) and the FRED API (optional key).

Data stored in macro_indicators table and automatically injected
as context into run_agent.py (Analyst Agent) on next run.

Tables:
  macro_indicators   — CPI/PPI/PCE monthly readings + YoY/MoM deltas
  macro_snapshots    — Claude-written macro regime summary

BLS Series used (no API key needed, 500 req/day limit):
  CUUR0000SA0   — CPI-U All Items (monthly)
  WPUFD4        — PPI Final Demand (monthly)
  PCU           — optionally added later

FRED (optional, set FRED_API_KEY in .env for higher limits):
  CPIAUCSL      — CPI
  PPIACO        — PPI

Usage:
  python run_macro.py
  python run_macro.py --dry-run
  python run_macro.py --source fred    (requires FRED_API_KEY in .env)
  python run_macro.py --source bls     (default, no key needed)
"""

import os, sys, json, re, argparse, uuid, datetime, time
import requests
import pandas as pd
from supabase import create_client
import anthropic

from llm import ask

from dotenv import load_dotenv
load_dotenv()

SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
FRED_KEY      = os.environ.get("FRED_API_KEY", "")  # optional

# Model and per-model request rules: llm.py.

# ── Schema SQL ─────────────────────────────────────────────────────────────────
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS macro_indicators (
    id            BIGSERIAL PRIMARY KEY,
    run_id        TEXT NOT NULL,
    series_id     TEXT NOT NULL,   -- e.g. CPIAUCSL
    series_name   TEXT,
    indicator     TEXT,            -- CPI / PPI / PCE
    period_date   DATE NOT NULL,
    value         NUMERIC(12,4),
    mom_change    NUMERIC(8,4),    -- month-over-month %
    yoy_change    NUMERIC(8,4),    -- year-over-year %
    source        TEXT,            -- BLS / FRED
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(series_id, period_date)
);

CREATE TABLE IF NOT EXISTS macro_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    run_id          TEXT NOT NULL,
    snapshot_date   DATE NOT NULL,
    macro_regime    TEXT,          -- INFLATIONARY / DISINFLATIONARY / DEFLATIONARY / STABLE
    cpi_latest      NUMERIC(8,4),
    cpi_yoy         NUMERIC(8,4),
    ppi_latest      NUMERIC(8,4),
    ppi_yoy         NUMERIC(8,4),
    regime_summary  TEXT,          -- Claude-written narrative
    equity_impact   TEXT,          -- POSITIVE / NEGATIVE / NEUTRAL
    prompt_tokens   INT,
    output_tokens   INT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(snapshot_date)
);
"""

# ── BLS fetcher (no key required) ─────────────────────────────────────────────

BLS_SERIES = {
    "CUUR0000SA0": ("CPI", "CPI-U All Items"),
    "WPUFD4":      ("PPI", "PPI Final Demand"),
}

def fetch_bls(series_ids: list, years_back: int = 3) -> dict[str, list]:
    """Fetch BLS series via public API v2 (no key, 500/day limit)."""
    current_year = datetime.date.today().year
    start_year   = current_year - years_back
    url = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
    payload = {
        "seriesid": series_ids,
        "startyear": str(start_year),
        "endyear": str(current_year),
        "calculations": True,
    }
    headers = {"Content-Type": "application/json"}
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS API error: {data.get('message', data)}")

    result = {}
    for series in data.get("Results", {}).get("series", []):
        sid = series["seriesID"]
        points = []
        for dp in series.get("data", []):
            year  = int(dp["year"])
            period = dp["period"]  # M01-M12
            if not period.startswith("M") or period == "M13":
                continue
            month = int(period[1:])
            date  = datetime.date(year, month, 1)
            calcs = dp.get("calculations", {})
            net   = calcs.get("net_changes", {})
            pct   = calcs.get("pct_changes", {})
            if dp["value"] == "-":
                continue
            points.append({
                "date":  date,
                "value": float(dp["value"]),
                # BLS calculations field is often absent for the most recent
                # month; store as placeholder and recompute below.
                "mom_change": float(pct.get("1",  0) or 0),
                "yoy_change": float(pct.get("12", 0) or 0),
            })
        pts = sorted(points, key=lambda x: x["date"])
        # Always recompute MoM / YoY from raw values so missing or zero
        # BLS calculation fields never propagate to the snapshot.
        for i, p in enumerate(pts):
            prev_1  = pts[i - 1]["value"]  if i >= 1  else None
            prev_12 = pts[i - 12]["value"] if i >= 12 else None
            p["mom_change"] = round((p["value"] - prev_1)  / prev_1  * 100, 3) if prev_1  else p["mom_change"]
            p["yoy_change"] = round((p["value"] - prev_12) / prev_12 * 100, 3) if prev_12 else p["yoy_change"]
        result[sid] = pts
    return result

# ── FRED fetcher (optional, higher rate limits) ────────────────────────────────

FRED_SERIES = {
    "CPIAUCSL": ("CPI", "CPI-U All Items SA"),
    "PPIACO":   ("PPI", "PPI All Commodities"),
}

def fetch_fred(series_id: str, api_key: str, limit: int = 24) -> list:
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id":      series_id,
        "api_key":        api_key,
        "file_type":      "json",
        "sort_order":     "desc",
        "limit":          limit,
        "observation_start": (datetime.date.today() - datetime.timedelta(days=730)).isoformat(),
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    obs = resp.json().get("observations", [])
    points = []
    for o in obs:
        val = o.get("value", ".")
        if val == ".":
            continue
        points.append({
            "date":  datetime.date.fromisoformat(o["date"]),
            "value": float(val),
        })
    points = sorted(points, key=lambda x: x["date"])

    # Compute MoM and YoY
    for i, p in enumerate(points):
        prev_1  = points[i - 1]["value"] if i >= 1  else None
        prev_12 = points[i - 12]["value"] if i >= 12 else None
        p["mom_change"] = round((p["value"] - prev_1)  / prev_1  * 100, 3) if prev_1  else 0.0
        p["yoy_change"] = round((p["value"] - prev_12) / prev_12 * 100, 3) if prev_12 else 0.0
    return points

# ── Claude macro regime ─────────────────────────────────────────────────────────

def claude_macro_regime(client, cpi_points: list, ppi_points: list) -> dict:
    """
    Calls Claude to classify the macro regime from CPI + PPI data.
    Returns a dict with macro_regime, equity_impact, regime_summary.
 
    Fixes vs original:
      - max_tokens raised 400 → 800 (prevents mid-sentence JSON truncation)
      - Fence stripping before JSON parse
      - Robust fallback that doesn't dump raw reasoning into regime_summary
    """
    def _fmt(pts, n=6):
        recent = pts[-n:] if len(pts) >= n else pts
        return [{"date": str(p["date"]), "value": p["value"],
                 "mom": p.get("mom_change"), "yoy": p.get("yoy_change")}
                for p in recent]
 
    import datetime
    prompt = f"""You are a macro economist analysing US inflation data for Orblex Industries.
 
CPI data (last 6 months):
{json.dumps(_fmt(cpi_points), indent=2)}
 
PPI data (last 6 months):
{json.dumps(_fmt(ppi_points), indent=2)}
 
Tasks:
1. Identify the macro regime: INFLATIONARY (CPI YoY >3.5%) / DISINFLATIONARY (falling but still elevated 2-3.5%) / STABLE (near 2% target) / DEFLATIONARY (<1%)
2. Assess whether PPI is leading CPI higher or lower (PPI leads CPI by ~1-2 months)
3. State the likely equity market impact: POSITIVE / NEGATIVE / NEUTRAL
 
CRITICAL OUTPUT RULES:
- Output ONLY a raw JSON object. No markdown, no backticks, no preamble.
- Do NOT show calculations or reasoning — conclusions only.
- Write complete sentences in regime_summary.
 
{{
  "macro_regime": "INFLATIONARY | DISINFLATIONARY | STABLE | DEFLATIONARY",
  "equity_impact": "POSITIVE | NEGATIVE | NEUTRAL",
  "regime_summary": "3-sentence narrative on current macro backdrop and equity implications."
}}"""
 
    msg = ask("fast", None, prompt, 1000, client)   # 400 truncated the JSON on Sonnet 4.6
 
    raw = msg.text.strip()
 
    # Strip markdown fences defensively
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'\s*```\s*$',        '', raw, flags=re.MULTILINE)
    raw = raw.strip()
 
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Try extracting JSON object from anywhere in the response
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            try:
                result = json.loads(match.group())
            except json.JSONDecodeError:
                result = {}
        else:
            result = {}
 
    # Ensure all fields present — never fall back to raw reasoning text
    result.setdefault("macro_regime",   "STABLE")
    result.setdefault("equity_impact",  "NEUTRAL")
    result.setdefault("regime_summary", "Macro regime data unavailable for this run.")
 
    result["prompt_tokens"] = msg.input_tokens
    result["output_tokens"] = msg.output_tokens
    return result

# ── Saver ──────────────────────────────────────────────────────────────────────

def build_indicator_rows(series_points: list, series_id: str, indicator: str, series_name: str, run_id: str, source: str) -> list:
    rows = []
    for p in series_points:
        rows.append({
            "run_id":      run_id,
            "series_id":   series_id,
            "series_name": series_name,
            "indicator":   indicator,
            "period_date": p["date"].isoformat(),
            "value":       p["value"],
            "mom_change":  p["mom_change"],
            "yoy_change":  p["yoy_change"],
            "source":      source,
        })
    return rows

def save_macro(sb, indicator_rows: list, snapshot_row: dict, dry_run: bool):
    if dry_run:
        print(f"  [DRY RUN] Would upsert {len(indicator_rows)} macro indicator rows")
        print(f"  [DRY RUN] Snapshot: {snapshot_row.get('macro_regime')} / {snapshot_row.get('equity_impact')}")
        return
    if indicator_rows:
        sb.table("macro_indicators").upsert(indicator_rows, on_conflict="series_id,period_date").execute()
    if snapshot_row:
        sb.table("macro_snapshots").upsert([snapshot_row], on_conflict="snapshot_date").execute()

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Orblex Industries — Macro Indicators Agent")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--source", choices=["bls", "fred"], default="bls",
                        help="Data source: bls (no key) or fred (requires FRED_API_KEY)")
    args = parser.parse_args()

    run_id  = args.run_id or f"macro-{datetime.date.today().isoformat()}-{uuid.uuid4().hex[:6]}"
    dry_run = args.dry_run
    source  = args.source

    if source == "fred" and not FRED_KEY:
        print("ERROR: FRED_API_KEY not set in .env. Use --source bls or set FRED_API_KEY.")
        sys.exit(1)

    print(f"\n{'='*55}")
    print(f"  Orblex Industries — Macro Indicators Agent")
    print(f"  Run ID : {run_id}")
    print(f"  Source : {source.upper()}")
    print(f"  Dry run: {dry_run}")
    print(f"{'='*55}\n")

    sb     = None if dry_run else create_client(SUPABASE_URL, SUPABASE_KEY)
    claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    all_rows = []
    cpi_points = []
    ppi_points = []

    if source == "bls":
        print("[BLS] Fetching CPI + PPI series...")
        try:
            series_data = fetch_bls(list(BLS_SERIES.keys()))
            for sid, (indicator, name) in BLS_SERIES.items():
                pts = series_data.get(sid, [])
                print(f"  [{sid}] {name}: {len(pts)} data points")
                rows = build_indicator_rows(pts, sid, indicator, name, run_id, "BLS")
                all_rows.extend(rows)
                if indicator == "CPI":
                    cpi_points = pts
                elif indicator == "PPI":
                    ppi_points = pts
        except Exception as e:
            print(f"  [BLS] ERROR: {e}")
            import traceback; traceback.print_exc()

    elif source == "fred":
        print("[FRED] Fetching CPI + PPI series...")
        for sid, (indicator, name) in FRED_SERIES.items():
            try:
                pts = fetch_fred(sid, FRED_KEY)
                print(f"  [{sid}] {name}: {len(pts)} data points")
                rows = build_indicator_rows(pts, sid, indicator, name, run_id, "FRED")
                all_rows.extend(rows)
                if indicator == "CPI":
                    cpi_points = pts
                elif indicator == "PPI":
                    ppi_points = pts
                time.sleep(0.5)
            except Exception as e:
                print(f"  [{sid}] ERROR: {e}")

    # Claude macro regime
    print("\n[Claude] Analysing macro regime...")
    regime = claude_macro_regime(claude, cpi_points, ppi_points)

    today = datetime.date.today()
    cpi_l = cpi_points[-1] if cpi_points else {}
    ppi_l = ppi_points[-1] if ppi_points else {}

    snapshot = {
        "run_id":         run_id,
        "snapshot_date":  today.isoformat(),
        "macro_regime":   regime.get("macro_regime"),
        "cpi_latest":     cpi_l.get("value"),
        "cpi_yoy":        cpi_l.get("yoy_change"),
        "ppi_latest":     ppi_l.get("value"),
        "ppi_yoy":        ppi_l.get("yoy_change"),
        "regime_summary": regime.get("regime_summary"),
        "equity_impact":  regime.get("equity_impact"),
        "prompt_tokens":  regime.get("prompt_tokens", 0),
        "output_tokens":  regime.get("output_tokens", 0),
    }

    print(f"  Regime : {snapshot['macro_regime']}")
    if snapshot["cpi_yoy"] == 0.0 or snapshot["cpi_yoy"] is None:
        print("  [WARN] CPI YoY is 0.0 — BLS may have returned <13 months of data.")
        print(f"  [WARN] CPI points fetched: {len(cpi_points)}")
    if snapshot["ppi_yoy"] == 0.0 or snapshot["ppi_yoy"] is None:
        print("  [WARN] PPI YoY is 0.0 — BLS may have returned <13 months of data.")
        print(f"  [WARN] PPI points fetched: {len(ppi_points)}")
    print(f"  Equity impact: {snapshot['equity_impact']}")

    save_macro(sb, all_rows, snapshot, dry_run)
    if not dry_run:
        print("\n✓ Macro data saved to Supabase ✓")

    print(f"\n✓ Macro agent complete. Run ID: {run_id}\n")

if __name__ == "__main__":
    main()