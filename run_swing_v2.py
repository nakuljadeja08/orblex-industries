"""
run_swing_v2.py — PIPELINE_V2 swing book in SHADOW mode (EXECUTOR_DESIGN.md phase 1).

  screen    swing_screen.screen_long on today's universe (rules only), then the
            swing_policy guards (stale data, earnings blackout, reward:risk)
  news      fast model per top candidate with headlines (none → no call)
  rank      one decision-model call over ≤ MAX_CANDIDATES candidates: order + veto
  save      swing_v2_picks (PICK / RESERVE / VETO / BLOCKED / DROPPED)

Shadow means it writes ONLY swing_v2_picks. It never touches agent_reports,
trade_orders or anything the morning plan reads, so v1 keeps trading unchanged
while both are logged for the A/B. Inverse setups are not screened live: the
2022–2026 replay showed no edge as specified (EXECUTOR_DESIGN.md §4).

Usage:
  python run_swing_v2.py --run-id <id> [--dry-run] [--no-llm] [--tickers A B C]
"""

import argparse
import logging
import sys
import uuid
from datetime import date

from config import load_config
from context_builder import (
    SWING_NEWS_DAYS, _fetch_news, _query_earnings, _query_metadata, _query_prices, query_spy_prices,
)
from loader import get_client
from swing_llm_v2 import apply_decisions, news_read, rank_and_veto
from swing_policy import SWING_LOOKBACK_DAYS, compute_swing_indicators, enforce_swing_rules
from swing_screen import rank, screen_long

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("swing-v2")

LIVE_FILTERS   = ()      # long filters switched on by replay evidence (swing_screen_backtest.py)
MAX_CANDIDATES = 6       # sent to the model; the rest are DROPPED
MAX_PICKS      = 2       # = the executor's new-entries-per-day limit


def _regime(sb) -> str | None:
    try:
        r = sb.table("market_regimes").select("regime").order("created_at", desc=True).limit(1).execute()
        return r.data[0]["regime"] if r.data else None
    except Exception as e:
        log.warning(f"  regime lookup failed: {e}")
        return None


def screen_today(sb, tickers: list[str], regime: str | None) -> tuple[list[dict], list[dict]]:
    """(candidates passing every rule, blocked rows with flags)."""
    spy = query_spy_prices(sb)
    cands, blocked = [], []
    for t in tickers:
        df = _query_prices(sb, t, days=SWING_LOOKBACK_DAYS)
        if df.empty or len(df) < 60:
            continue
        ind = compute_swing_indicators(df, spy)
        plan, _ = screen_long(t, ind, regime, LIVE_FILTERS)
        if not plan:
            continue
        a = {"signal": "BUY", "signal_strength": "MODERATE", **{k: plan[k] for k in
             ("entry_price", "stop_price", "target_price", "horizon_days")}}
        enforce_swing_rules(a, ind, _query_earnings(sb, t))
        plan.update(flags=a["swing_flags"], notes=a["swing_notes"],
                    trading_days_to_earnings=a.get("trading_days_to_earnings"),
                    rsi_14=ind.get("rsi_14"), price_vs_ma20_pct=ind.get("price_vs_ma20_pct"),
                    volume_ratio=ind.get("volume_ratio"))
        if a["signal"] != "BUY":
            blocked.append({**plan, "status": "BLOCKED"})
            continue
        plan.update(horizon_days=a["horizon_days"])   # may be clamped before earnings
        cands.append(plan)
    return rank(cands), blocked


def llm_pass(sb, cands: list[dict], regime: str | None) -> tuple[list[dict], str, int]:
    tokens = 0
    for c in cands:
        meta = _query_metadata(sb, c["ticker"])
        heads = _fetch_news(c["ticker"], meta.get("company_name"), days=SWING_NEWS_DAYS, sort_by="publishedAt")
        try:
            c["news"] = news_read(c["ticker"], meta.get("company_name"), heads)
        except Exception as e:
            c["news"] = {"catalyst": None, "veto": False, "reason": f"news read failed: {e}"[:200], "tokens": 0}
        tokens += c["news"].get("tokens", 0)
    try:
        decisions, t = rank_and_veto(cands, regime)
        return apply_decisions(cands, decisions), "OK", tokens + t
    except Exception as e:
        log.warning(f"  rank/veto call failed, keeping script order: {e}")
        return apply_decisions(cands, []), f"ERROR: {e}"[:200], tokens


def build_rows(run_id: str, today: date, regime, sent: list[dict], dropped: list[dict],
               blocked: list[dict], llm_status: str, tokens: int) -> list[dict]:
    script_top = {c["ticker"] for c in sorted(sent, key=lambda c: c.get("script_rank", 0))[:MAX_PICKS]}
    rows, kept = [], 0
    for c in sent:
        if c.get("llm_decision") == "VETO":
            status = "VETO"
        else:
            status = "PICK" if kept < MAX_PICKS else "RESERVE"
            kept += 1
        rows.append({**c, "status": status})
    rows += [{**c, "status": "DROPPED"} for c in dropped] + blocked
    out = []
    for i, r in enumerate(rows):
        out.append({
            "run_id": run_id, "report_date": today.isoformat(), "ticker": r["ticker"],
            "setup_type": r["setup_type"], "regime": regime,
            "entry_price": r["entry_price"], "stop_price": r["stop_price"], "target_price": r["target_price"],
            "horizon_days": r["horizon_days"], "risk_mult": r["risk_mult"], "risk_reward": r.get("risk_reward"),
            "rank_score": r.get("rank_score"), "script_rank": r.get("script_rank"),
            "script_pick": r["ticker"] in script_top,
            "news_catalyst": (r.get("news") or {}).get("catalyst"),
            "news_reason": (r.get("news") or {}).get("reason"),
            "llm_decision": r.get("llm_decision"), "llm_rank": r.get("llm_rank"),
            "llm_reason": r.get("llm_reason"), "llm_stop_from": r.get("llm_stop_from"),
            "llm_status": llm_status if r["status"] in ("PICK", "RESERVE", "VETO") else None,
            "status": r["status"], "flags": r.get("flags") or [], "notes": r.get("notes") or [],
            "tokens": tokens if i == 0 else None,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="PIPELINE_V2 swing book (shadow)")
    ap.add_argument("--run-id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-llm", action="store_true", help="script only (no model calls)")
    ap.add_argument("--tickers", nargs="+")
    args = ap.parse_args()

    cfg = load_config()
    sb = get_client(cfg.supabase_url, cfg.supabase_key)
    run_id = args.run_id or f"swingv2-{date.today().isoformat()}-{uuid.uuid4().hex[:6]}"
    tickers = [t.upper() for t in (args.tickers or cfg.tickers)]
    regime = _regime(sb)
    log.info(f"PIPELINE_V2 swing (shadow) · {len(tickers)} tickers · regime {regime} · run {run_id}")

    cands, blocked = screen_today(sb, tickers, regime)
    sent, dropped = cands[:MAX_CANDIDATES], cands[MAX_CANDIDATES:]
    for i, c in enumerate(sent):
        c["script_rank"] = i
    log.info(f"  screen: {len(cands)} pass, {len(blocked)} blocked by rules; {len(sent)} to the model")

    if args.no_llm or not sent:
        llm_status, tokens = "SKIPPED", 0
        sent = [{**c, "llm_decision": "KEEP"} for c in sent]
    else:
        sent, llm_status, tokens = llm_pass(sb, sent, regime)

    rows = build_rows(run_id, date.today(), regime, sent, dropped, blocked, llm_status, tokens)
    for r in rows:
        extra = f" · {r['llm_reason']}" if r.get("llm_reason") else ""
        log.info(f"  {r['status']:<8} {r['ticker']:<6} {r['setup_type']:<14} entry {r['entry_price']} "
                 f"stop {r['stop_price']} target {r['target_price']}"
                 f"{' · script pick' if r['script_pick'] else ''}{extra}")
    log.info(f"  model: {llm_status} · {tokens} tokens")

    if not args.dry_run and rows:
        sb.table("swing_v2_picks").upsert(rows, on_conflict="run_id,ticker").execute()
        log.info(f"  saved {len(rows)} swing_v2_picks rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
