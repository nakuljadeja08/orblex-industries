"""
sync_fills.py — links orders placed by hand in the broker app back to the
approved trade_orders they came from, and records their fills.

Under the dashboard-approval flow nobody calls the broker for us: the user
approves an order on the dashboard, then places it in the Robinhood app. This
sync (run by the scheduled Claude task, with the broker's order list fetched
read-only through the MCP) closes the loop:

  match    an APPROVED order ↔ a broker order with the same ticker, side and
           quantity, a compatible type at the same price (limit price for
           LIMIT, stop price for STOP), created on/after the plan date and not
           already linked. Earliest match wins. → PLACED + broker_order_id
  fills    for every linked order, broker cumulative_quantity minus what is
           already in trade_fills becomes a new fill row → PARTIAL / FILLED
  closed   a linked broker order cancelled/rejected/failed with nothing filled
           → CANCELLED (a partial stays PARTIAL — those shares are held)
  expire   an APPROVED DAY order from an earlier session that was never placed
           → EXPIRED
  untracked  broker orders with fills that match nothing of ours are reported;
           the guard still sees them through the account snapshot.

Usage:
  python sync_fills.py --orders rh_orders.json [--dry-run]
"""

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from broker import ref_id

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")
DEAD_STATES = {"cancelled", "rejected", "failed", "voided"}
PRICE_TOL = 0.005


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _ts(x):
    return datetime.fromisoformat(str(x).replace("Z", "+00:00")) if x else None


def _broker_kind(b: dict) -> tuple[str, float | None]:
    """(LIMIT | MARKET | STOP | STOP_LIMIT, the price that identifies it)."""
    t, trig = (b.get("type") or "").lower(), (b.get("trigger") or "").lower()
    if trig == "stop" or t in ("stop_market", "stop_limit"):
        return ("STOP_LIMIT" if t in ("limit", "stop_limit") else "STOP"), _f(b.get("stop_price"))
    if t == "limit":
        return "LIMIT", _f(b.get("price"))
    return "MARKET", None


def _matches(o: dict, b: dict) -> bool:
    if (b.get("symbol") or "").upper() != o["ticker"] or (b.get("side") or "").upper() != o["side"]:
        return False
    if abs((_f(b.get("quantity"), 0) or 0) - float(o["qty"])) > 1e-6:
        return False
    created = _ts(b.get("created_at"))
    if created and created.astimezone(NY).date() < date.fromisoformat(str(o["plan_date"])[:10]):
        return False
    kind, px = _broker_kind(b)
    want = o["order_type"]
    if want == "LIMIT":
        return kind == "LIMIT" and px is not None and abs(px - float(o["limit_price"])) <= PRICE_TOL
    if want == "STOP":
        return kind in ("STOP", "STOP_LIMIT") and px is not None and abs(px - float(o["stop_price"])) <= PRICE_TOL
    return kind == "MARKET"


def plan_sync(ours: list[dict], broker: list[dict], recorded: dict, today: date,
              known: set | None = None) -> dict:
    """
    Pure: decide every change. ours = trade_orders rows (APPROVED / PLACED /
    PARTIAL); broker = MCP order dicts; recorded = {order_id: qty already in
    trade_fills}. Returns {"link": [...], "fills": [...], "status": {...},
    "untracked": [...]}.
    """
    by_id = {b.get("id"): b for b in broker}
    out = {"link": [], "fills": [], "status": {}, "untracked": []}
    # Orders the executor sent carry ref_id = uuid5("wi-<id>"): link them by that, whatever id
    # the ledger holds (a pending approval's id until you approve it in the app).
    by_ref = {str(b.get("ref_id")).lower(): b for b in broker if b.get("ref_id")}
    relinked = []
    for o in ours:
        b = by_ref.get(ref_id(f"wi-{o['id']}"))
        if b and o.get("broker_order_id") != b.get("id"):
            out["link"].append((o["id"], b["id"]))
            if o["status"] == "APPROVED":
                out["status"][o["id"]] = "PLACED"
            o = {**o, "broker_order_id": b["id"]}
        relinked.append(o)
    ours = relinked
    # `known`: broker ids linked to ANY plan order (incl. FILLED/CANCELLED ones not in `ours`),
    # so a finished order is never reported as untracked again on later runs.
    linked = {o["broker_order_id"] for o in ours if o.get("broker_order_id")} | set(known or ())

    for o in sorted(ours, key=lambda x: x["id"]):
        if o.get("broker_order_id") or o["status"] != "APPROVED":
            continue
        # A cancelled-and-re-placed order (e.g. GTC fixed to DAY) leaves a dead twin with the same
        # ticker, qty and price: prefer the order that is still live or has fills.
        cands = sorted((b for b in broker if b.get("id") not in linked and _matches(o, b)),
                       key=lambda b: ((b.get("state") or "").lower() in DEAD_STATES
                                      and not (_f(b.get("cumulative_quantity"), 0) or 0), b.get("created_at") or ""))
        if cands:
            b = cands[0]
            linked.add(b["id"])
            o = {**o, "broker_order_id": b["id"]}
            out["link"].append((o["id"], b["id"]))
            out["status"][o["id"]] = "PLACED"
        elif o.get("time_in_force", "DAY") == "DAY" and str(o["plan_date"])[:10] < today.isoformat():
            out["status"][o["id"]] = "EXPIRED"
            continue
        else:
            continue
        ours = [x if x["id"] != o["id"] else o for x in ours]

    for o in ours:
        b = by_id.get(o.get("broker_order_id"))
        if not b:
            continue
        cum = _f(b.get("cumulative_quantity"), 0) or 0
        new = round(cum - float(recorded.get(o["id"], 0)), 6)
        if new > 1e-9:
            when = _ts(b.get("last_transaction_at") or b.get("updated_at")) or datetime.now(timezone.utc)
            out["fills"].append({"order_id": o["id"], "ticker": o["ticker"], "side": o["side"], "qty": new,
                                 "price": _f(b.get("average_price")), "filled_at": when.isoformat(),
                                 "trade_date": when.astimezone(NY).date().isoformat(),
                                 "broker_order_id": b["id"], "source": "robinhood"})
        if cum >= float(o["qty"]) - 1e-9:
            out["status"][o["id"]] = "FILLED"
        elif cum > 0:
            out["status"][o["id"]] = "PARTIAL"
        elif (b.get("state") or "").lower() in DEAD_STATES:
            out["status"][o["id"]] = "CANCELLED"

    for b in broker:
        if b.get("id") not in linked and (_f(b.get("cumulative_quantity"), 0) or 0) > 0:
            u = {"id": b.get("id"), "symbol": b.get("symbol"), "side": b.get("side"),
                 "qty": b.get("cumulative_quantity"), "at": b.get("last_transaction_at")}
            near = near_miss(ours, b)
            if near:
                u["near_order"], u["plan_price"], u["placed_price"] = near
            out["untracked"].append(u)
    return out


def near_miss(ours: list[dict], b: dict):
    """(order id, plan price, placed price) when a broker order matches an approved plan
    order in everything but its price — e.g. a limit typed above the plan's. It is still
    NOT linked (prices must match exactly), but the report names the plan order."""
    kind, px = _broker_kind(b)
    for o in ours:
        if ((b.get("symbol") or "").upper() == o["ticker"] and (b.get("side") or "").upper() == o["side"]
                and abs((_f(b.get("quantity"), 0) or 0) - float(o["qty"])) <= 1e-6 and o["order_type"] == kind):
            want = o.get("limit_price") if kind == "LIMIT" else o.get("stop_price")
            if want is not None and px is not None and abs(px - float(want)) > PRICE_TOL:
                return o["id"], float(want), px
    return None


def main():
    ap = argparse.ArgumentParser(description="Link hand-placed broker orders to approved trade_orders and record fills")
    ap.add_argument("--orders", required=True, help="get_equity_orders response JSON (recent orders)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from config import load_config
    from loader import get_client

    with open(args.orders, encoding="utf-8") as f:
        raw = json.load(f)
    broker = (raw.get("data", raw) or {}).get("orders", [])

    cfg = load_config()
    sb = get_client(cfg.supabase_url, cfg.supabase_key)
    ours = (sb.table("trade_orders").select("*")
            .in_("status", ["APPROVED", "PLACED", "PARTIAL"]).execute().data or [])
    ids = [o["id"] for o in ours]
    recorded: dict = {}
    if ids:
        for r in sb.table("trade_fills").select("order_id,qty").in_("order_id", ids).execute().data or []:
            recorded[r["order_id"]] = recorded.get(r["order_id"], 0) + float(r["qty"])

    known = {r["broker_order_id"] for r in (sb.table("trade_orders").select("broker_order_id")
                                             .not_.is_("broker_order_id", "null").execute().data or [])}
    today = datetime.now(NY).date()
    plan = plan_sync(ours, broker, recorded, today, known)

    for oid, bid in plan["link"]:
        logger.info(f"  link    order #{oid} ↔ broker {bid}")
    for fl in plan["fills"]:
        logger.info(f"  fill    order #{fl['order_id']} {fl['side']} {fl['qty']:g} {fl['ticker']} @ {fl['price']} ({fl['trade_date']})")
    for oid, st in plan["status"].items():
        logger.info(f"  status  order #{oid} → {st}")
    for u in plan["untracked"]:
        if u.get("near_order"):
            logger.info(f"  UNTRACKED broker order {u['id']}: {u['side']} {u['qty']} {u['symbol']} at {u['at']} "
                        f"— matches approved order #{u['near_order']} except the price: plan {u['plan_price']:g}, "
                        f"placed {u['placed_price']:g}. Not linked; its fill is not in the ledger.")
        else:
            logger.info(f"  UNTRACKED broker order {u['id']}: {u['side']} {u['qty']} {u['symbol']} at {u['at']} "
                        f"— not from an approved plan")
    if not any(plan.values()):
        logger.info("  Nothing to sync.")

    if args.dry_run:
        logger.info("[DRY RUN] Nothing written.")
        return
    now = datetime.now(timezone.utc).isoformat()
    for oid, bid in plan["link"]:
        sb.table("trade_orders").update({"broker_order_id": bid, "updated_at": now}).eq("id", oid).execute()
    if plan["fills"]:
        sb.table("trade_fills").insert(plan["fills"]).execute()
    for oid, st in plan["status"].items():
        sb.table("trade_orders").update({"status": st, "updated_at": now}).eq("id", oid).execute()


if __name__ == "__main__":
    main()
