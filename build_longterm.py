"""
build_longterm.py — turns a long-term (position-mode) pipeline run into the week's advice
for the MAIN account (EXECUTOR_DESIGN.md §2.1). Rules only, no model calls.

For every ticker in the run's allocation:
  target     allocation_details.allocated_pct (after the enforcer's caps)
  action     against the main account's current weight when a holdings snapshot
             exists (basis HOLDINGS), otherwise against last week's target
             (basis LAST_WEEK): NEW / ADD / TRIM / EXIT / HOLD, or AVOID for a
             name the plan gives 0% and you don't hold, with a
             HOLD_BAND percentage-point dead zone so tiny drifts don't churn
  conviction 3 / 2 / 1 from the analyst's STRONG / MODERATE / WEAK
  why        ≤ 3 short bullets, each tagged with its source (Analyst,
             Valuation, Smart money, Risk)
  wrong if   one concrete line that would change the call
  changes    what moved since last week (new name, action flip, target move,
             conviction change)

Writes longterm_advice. Nothing here is an order, and the main account is
never traded by this system.

Usage:
  python build_longterm.py --run-id pipeline-2026-10-18-abc123 [--dry-run]
  python build_longterm.py --latest --dry-run          # newest position-mode run
"""

import argparse
import logging
import re
import sys
from datetime import date, timedelta
from typing import Optional

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("longterm")

HOLD_BAND = 1.0          # percentage points: |target − current| below this is HOLD
TARGET_MOVE = 1.0        # a target move this large is listed under "what changed"
CONVICTION = {"STRONG": 3, "MODERATE": 2, "WEAK": 1}
MAX_WHY = 3


def week_of(d: date) -> date:
    """The Monday of the week the advice is for (a Saturday run advises the coming week)."""
    return d + timedelta(days=(7 - d.weekday()) % 7) if d.weekday() >= 5 else d - timedelta(days=d.weekday())


def decide(target: float, current: Optional[float]) -> str:
    current = current or 0.0
    if target <= 0 and current <= 0:
        return "AVOID"
    if current <= 0:
        return "NEW"
    if target <= 0:
        return "EXIT"
    gap = target - current
    if abs(gap) < HOLD_BAND:
        return "HOLD"
    return "ADD" if gap > 0 else "TRIM"


def first_sentence(text: Optional[str], limit: int = 160) -> Optional[str]:
    if not text:
        return None
    s = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def why_bullets(rep: dict, det: dict, fund: dict, smart: dict) -> list[dict]:
    out = []
    sig = rep.get("signal")
    if sig:
        reason = first_sentence(rep.get("reasoning")) or first_sentence(rep.get("summary"))
        out.append({"source": "Analyst", "text": f"{sig} {rep.get('signal_strength') or ''}".strip()
                    + (f": {reason}" if reason else "")})
    val = rep.get("valuation_view")
    if val and val not in ("N/A", "UNKNOWN"):
        bits = []
        pe, peg = fund.get("forward_pe"), fund.get("peg_ratio")
        if pe is not None and float(pe) <= 0:
            bits.append("not profitable on forward estimates")   # a negative P/E is not a valuation
        else:
            if pe:
                bits.append(f"forward P/E {float(pe):.1f}")
            if peg and float(peg) > 0:
                bits.append(f"PEG {float(peg):.2f}")
        out.append({"source": "Valuation", "text": val.replace("_", " ").lower().capitalize()
                    + (f" ({', '.join(bits)})" if bits else "")})
    if smart.get("holders"):
        moves = [f"{n} {k.lower()}" for k, n in (("NEW", smart.get("NEW", 0)), ("ADDED", smart.get("ADDED", 0)),
                 ("REDUCED", smart.get("REDUCED", 0)), ("SOLD", smart.get("SOLD", 0))) if n]
        out.append({"source": "Smart money", "text": f"{smart['holders']} tracked fund(s) hold it"
                    + (f"; last quarter {', '.join(moves)}" if moves else "") + f" (13F {smart.get('period')})"})
    if det.get("risk_score") is not None and len(out) < MAX_WHY:
        flags = ", ".join(f.replace("_", " ").lower() for f in (det.get("risk_flags") or [])[:2])
        out.append({"source": "Risk", "text": f"risk {det['risk_score']}/10" + (f": {flags}" if flags else "")})
    return out[:MAX_WHY]


def wrong_if(action: str, rep: dict, det: dict) -> str:
    if rep.get("invalidation"):
        return rep["invalidation"]
    if action in ("ADD", "NEW", "HOLD") and (det.get("allocated_pct") or 0) > 0:
        return "the analyst turns SELL or risk rises to 8+/10 (excluded)"
    if action in ("TRIM", "EXIT", "AVOID"):
        return "the analyst turns BUY STRONG with risk under 6/10"
    return "the analyst turns BUY with risk under 8/10"


def changes_since(row: dict, prev: Optional[dict]) -> list[str]:
    if prev is None:
        return ["new this week"]
    out = []
    if prev.get("action") != row["action"]:
        out.append(f"action {prev.get('action')} → {row['action']}")
    pt = float(prev.get("target_pct") or 0)
    if abs(row["target_pct"] - pt) >= TARGET_MOVE:
        out.append(f"target {pt:.1f}% → {row['target_pct']:.1f}%")
    if prev.get("conviction") and row.get("conviction") and prev["conviction"] != row["conviction"]:
        out.append(f"conviction {'up' if row['conviction'] > prev['conviction'] else 'down'}")
    if prev.get("signal") and prev["signal"] != row.get("signal"):
        out.append(f"signal {prev['signal']} → {row.get('signal')}")
    return out


def build_advice(run_id: str, run_date: date, reports: dict, details: list[dict], funds: dict,
                 smart: dict, prev: dict, holdings: Optional[dict], regime: Optional[str],
                 cash_pct: Optional[float]) -> list[dict]:
    """Pure: one advice row per allocation ticker, plus EXIT rows for held names the plan dropped."""
    basis = "HOLDINGS" if holdings is not None else "LAST_WEEK"
    wk = week_of(run_date)
    rows = []
    seen = set()
    for det in details:
        t = det["ticker"]
        seen.add(t)
        rep = reports.get(t, {})
        target = round(float(det.get("allocated_pct") or 0), 2)
        p = prev.get(t)
        current = holdings.get(t, 0.0) if holdings is not None else (float(p["target_pct"]) if p else 0.0)
        action = decide(target, current)
        row = {
            "run_id": run_id, "week_of": wk.isoformat(), "ticker": t, "action": action,
            "target_pct": target, "prev_target_pct": float(p["target_pct"]) if p else None,
            "basis": basis, "conviction": CONVICTION.get(rep.get("signal_strength") or det.get("signal_strength")),
            "signal": rep.get("signal") or det.get("analyst_signal"),
            "signal_strength": rep.get("signal_strength") or det.get("signal_strength"),
            "valuation_view": rep.get("valuation_view"), "risk_score": det.get("risk_score"),
            "sector": det.get("sector"),
            "why": why_bullets(rep, det, funds.get(t, {}), smart.get(t, {})),
            "regime": regime, "cash_pct": cash_pct,
        }
        row["wrong_if"] = wrong_if(action, rep, det)
        row["changes"] = changes_since(row, p)
        rows.append(row)
    for t, w in (holdings or {}).items():
        if t not in seen and w > 0:
            rows.append({
                "run_id": run_id, "week_of": wk.isoformat(), "ticker": t, "action": "EXIT", "target_pct": 0.0,
                "prev_target_pct": None, "basis": basis, "conviction": None, "signal": None,
                "signal_strength": None, "valuation_view": None, "risk_score": None, "sector": None,
                "why": [{"source": "Plan", "text": "held, but not in the long-term universe this week"}],
                "wrong_if": "you mean to hold it outside this plan (add it to universe.txt)",
                "changes": [], "regime": regime, "cash_pct": cash_pct,
            })
    order = {"NEW": 0, "ADD": 1, "TRIM": 2, "EXIT": 3, "HOLD": 4, "AVOID": 5}
    return sorted(rows, key=lambda r: (order[r["action"]], -r["target_pct"]))


# ── I/O ────────────────────────────────────────────────────────────────────────

def _latest_position_run(sb) -> Optional[str]:
    r = (sb.table("agent_reports").select("run_id,trading_mode").or_("trading_mode.is.null,trading_mode.eq.position")
         .order("created_at", desc=True).limit(1).execute().data)
    return r[0]["run_id"] if r else None


def _rows(query) -> list[dict]:
    """Rows, or [] when the table isn't there yet (longterm_schema.sql not applied)."""
    try:
        return query.execute().data or []
    except Exception as e:
        if "PGRST205" in str(e) or "does not exist" in str(e):
            return []
        raise


def load_inputs(sb, run_id: str) -> dict:
    reps = sb.table("agent_reports").select("*").eq("run_id", run_id).execute().data or []
    if not reps:
        raise SystemExit(f"no agent_reports for {run_id}")
    if any(r.get("trading_mode") == "swing" for r in reps):
        raise SystemExit(f"{run_id} is a swing run; the long-term book needs a position-mode run")
    run_date = date.fromisoformat(str(reps[0]["report_date"])[:10])
    plan = (sb.table("allocation_plans").select("run_id,cash_pct").eq("analyst_run_id", run_id)
            .order("created_at", desc=True).limit(1).execute().data)
    if not plan:
        raise SystemExit(f"no allocation plan for {run_id}")
    details = sb.table("allocation_details").select("*").eq("plan_run_id", plan[0]["run_id"]).execute().data or []
    tickers = [d["ticker"] for d in details]

    funds = {}
    for f in (sb.table("stock_fundamentals").select("ticker,forward_pe,peg_ratio,snapshot_date")
              .in_("ticker", tickers).order("snapshot_date", desc=True).limit(2000).execute().data or []):
        funds.setdefault(f["ticker"], f)

    smart: dict = {}
    hold = (sb.table("investor_holdings").select("ticker,manager,change_type,period_of_report")
            .in_("ticker", tickers).order("period_of_report", desc=True).limit(5000).execute().data or [])
    latest_period = {}
    for h in hold:
        latest_period.setdefault(h["ticker"], h["period_of_report"])
    for h in hold:
        if h["period_of_report"] != latest_period[h["ticker"]]:
            continue
        s = smart.setdefault(h["ticker"], {"holders": 0, "period": h["period_of_report"]})
        if h["change_type"] != "SOLD":
            s["holders"] += 1
        s[h["change_type"]] = s.get(h["change_type"], 0) + 1

    prev_rows = _rows(sb.table("longterm_advice").select("*").lt("week_of", week_of(run_date).isoformat())
                      .order("week_of", desc=True).limit(500))
    prev_week = prev_rows[0]["week_of"] if prev_rows else None
    prev = {r["ticker"]: r for r in prev_rows if r["week_of"] == prev_week}

    snap = _rows(sb.table("longterm_holdings").select("snapshot_date").order("snapshot_date", desc=True).limit(1))
    holdings = None
    if snap and date.fromisoformat(snap[0]["snapshot_date"]) >= run_date - timedelta(days=10):
        holdings = {h["ticker"]: float(h["weight_pct"]) for h in
                    sb.table("longterm_holdings").select("ticker,weight_pct")
                    .eq("snapshot_date", snap[0]["snapshot_date"]).execute().data or []}

    reg = sb.table("market_regimes").select("regime").order("created_at", desc=True).limit(1).execute().data
    return {"run_date": run_date, "reports": {r["ticker"]: r for r in reps}, "details": details,
            "funds": funds, "smart": smart, "prev": prev, "holdings": holdings,
            "regime": reg[0]["regime"] if reg else None, "cash_pct": plan[0].get("cash_pct")}


def main() -> int:
    from config import load_config
    from loader import get_client
    ap = argparse.ArgumentParser(description="Build the week's long-term advice from a position-mode run")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-id")
    g.add_argument("--latest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    sb = get_client(cfg.supabase_url, cfg.supabase_key)
    run_id = args.run_id or _latest_position_run(sb)
    if not run_id:
        log.info("No position-mode run found.")
        return 1
    x = load_inputs(sb, run_id)
    rows = build_advice(run_id, x["run_date"], x["reports"], x["details"], x["funds"], x["smart"],
                        x["prev"], x["holdings"], x["regime"], x["cash_pct"])
    basis = rows[0]["basis"] if rows else "-"
    log.info(f"Long-term advice · run {run_id} · week of {week_of(x['run_date'])} · basis {basis} · {len(rows)} tickers")
    for r in rows:
        log.info(f"  {r['action']:<5} {r['ticker']:<6} target {r['target_pct']:>5.1f}%  "
                 f"{'●' * (r['conviction'] or 0):<3} {'; '.join(r['changes'])}")
    if not args.dry_run and rows:
        sb.table("longterm_advice").upsert(rows, on_conflict="run_id,ticker").execute()
        log.info(f"Saved {len(rows)} longterm_advice rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
