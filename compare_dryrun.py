"""
compare_dryrun.py — phase 4 gate data: after the close, compare what the executor WOULD have
done this morning (exec_dryrun, from run_dryrun.py) with what was actually placed by hand.

  MATCH             both: the executor's order and a placed order linked to the same plan order
  YOU SKIPPED       the executor would have submitted it; nothing was placed
  EXECUTOR REFUSED  placed by hand, but the executor refused it (its reasons are listed)
  OFF-PLAN          a broker order today that no plan order is linked to (a different price
                    than approved, or not in the plan at all)

Writes the result into the day's latest exec_dryrun row (comparison) and prints it.

Usage:
  python compare_dryrun.py [--date 2026-10-16] --orders .trading\\2026-10-16\\eod-orders.json
"""

import argparse
import json
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from config import load_config
from loader import get_client

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

NY = ZoneInfo("America/New_York")
LIVE_OR_DONE = {"queued", "confirmed", "unconfirmed", "partially_filled", "filled"}


def compare(run: dict, today_orders: list[dict], broker_orders: list[dict], day: date) -> dict:
    """Pure. run = exec_dryrun row; today_orders = trade_orders rows with plan_date = day;
    broker_orders = raw get_equity_orders rows."""
    would = {(w["ticker"], w["side"]): w for w in run.get("would") or [] if w.get("action") == "SUBMIT"}
    refused = {}
    for r in run.get("refusals") or []:
        refused.setdefault(r["ticker"], set()).update(r["reasons"])
    linked = {o["broker_order_id"]: o for o in today_orders if o.get("broker_order_id")}
    placed = {(o["ticker"], o["side"]): o for o in linked.values()}

    rows = []
    for (t, side), w in sorted(would.items()):
        if (t, side) in placed:
            rows.append({"ticker": t, "side": side, "result": "MATCH", "would": w,
                         "placed_status": placed[(t, side)]["status"]})
        else:
            rows.append({"ticker": t, "side": side, "result": "YOU SKIPPED", "would": w})
    for (t, side), o in sorted(placed.items()):
        if (t, side) not in would:
            rows.append({"ticker": t, "side": side, "result": "EXECUTOR REFUSED" if t in refused else "NOT IN DRY RUN",
                         "reasons": sorted(refused.get(t, [])), "placed_status": o["status"], "intent": o["intent"]})
    for b in broker_orders:
        created = b.get("created_at")
        if not created or datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(NY).date() != day:
            continue
        if b.get("id") in linked or (b.get("state") or "").lower() not in LIVE_OR_DONE:
            continue
        rows.append({"ticker": (b.get("symbol") or "").upper(), "side": (b.get("side") or "").upper(),
                     "result": "OFF-PLAN", "type": b.get("type"), "price": b.get("price"),
                     "stop_price": b.get("stop_price"), "qty": b.get("quantity"), "state": b.get("state")})
    counts = {}
    for r in rows:
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    return {"date": day.isoformat(), "dryrun_status": run.get("status"), "mandate_source": run.get("mandate_source"),
            "counts": counts, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare the morning dry run with what was actually placed")
    ap.add_argument("--date")
    ap.add_argument("--orders", required=True, help="raw get_equity_orders JSON saved after the close")
    args = ap.parse_args()
    day = date.fromisoformat(args.date) if args.date else datetime.now(NY).date()
    cfg = load_config()
    sb = get_client(cfg.supabase_url, cfg.supabase_key)

    runs = (sb.table("exec_dryrun").select("*").eq("run_date", day.isoformat())
            .order("run_at", desc=True).limit(1).execute().data)
    if not runs:
        print(f"No dry run recorded for {day}.")
        return 1
    run = runs[0]
    todays = sb.table("trade_orders").select("*").eq("plan_date", day.isoformat()).execute().data or []
    raw = json.load(open(args.orders, encoding="utf-8"))
    data = raw.get("data", raw)
    res = compare(run, todays, data.get("orders") or data.get("results") or [], day)
    sb.table("exec_dryrun").update({"comparison": res}).eq("id", run["id"]).execute()

    print(f"Dry run vs actual, {day} · dry run {res['dryrun_status']} · mandate {res['mandate_source']} · {res['counts'] or 'nothing either way'}")
    for r in res["rows"]:
        extra = ""
        if r["result"] == "EXECUTOR REFUSED":
            extra = f" — refused: {', '.join(r['reasons'])}"
        elif r["result"] == "OFF-PLAN":
            extra = f" — {r.get('type')} {r.get('qty')} @ {r.get('price') or r.get('stop_price')} ({r.get('state')})"
        elif r["result"] in ("MATCH", "YOU SKIPPED"):
            w = r["would"]
            extra = f" — {w['type']} {w['qty']}" + (f" limit {w['limit']}" if w.get("limit") else "")
        print(f"  {r['result']:<17} {r['side']:<4} {r['ticker']:<6}{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
