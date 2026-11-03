"""
run_executor.py — phase 5 (EXECUTOR_DESIGN.md §5): the executor against the LIVE Robinhood
account, sending real orders into Robinhood's in-app APPROVAL QUEUE. Nothing executes until
you tap approve in the app; ignored approvals expire on their own.

It refuses to start unless EXECUTOR_LIVE=1 (a GitHub repository variable; only you set it).
With the switch off, jobs.py runs the phase 4 dry run instead (run_dryrun.py).

Differences from the dry run:
  • entries: only the ones you APPROVED on the dashboard (PROPOSED ones are ignored)
  • mandate: your dashboard mandate for today, or none, in which case every entry is
    refused (exits and protective OCOs still go). Nothing is ASSUMED.
  • Robinhood's approval setting must read human_must_approve_trades = true in this same
    session, or no order is sent (rh_trader)
  • ledger writes go straight to Supabase: executor-made orders (protective OCOs, time and
    stop exits) are inserted into trade_orders, each send is an exec_events row, and
    PLACED / CANCELLED / broker_order_id are written as they happen. Fills are NOT written
    here: sync_fills.py records them (jobs.py runs it before and after this).
  • one pass, not two

Usage:
  python run_executor.py
"""

import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from executor import Executor, MemoryLedger, _oid
from rh_trader import SWITCH, TradingRobinhood, live_switch_on
from robinhood_broker import LiveRobinhoodBroker
from run_dryrun import ACCOUNT, calendar, snapshot_ledger, today_ny

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass
logging.basicConfig(level=logging.WARNING, format="%(message)s")

NY = ZoneInfo("America/New_York")
OUT = Path(".trading/live")
PERSISTED_STATUSES = {"PLACED", "CANCELLED"}       # FILLED / PARTIAL / EXPIRED belong to sync_fills


@dataclass
class SupabaseLedger(MemoryLedger):
    """MemoryLedger that writes the executor's own changes through to Supabase as they happen."""
    sb: object = None
    plan_id: str = ""

    def add_order(self, row: dict) -> dict:
        rec = {"plan_id": self.plan_id, "plan_date": row["plan_date"], "ticker": row["ticker"], "side": row["side"],
               "intent": row["intent"], "order_type": row["order_type"], "time_in_force": row.get("time_in_force", "DAY"),
               "qty": row["qty"], "stop_price": row.get("stop_price"), "target_price": row.get("target_price"),
               "status": "APPROVED", "account_source": "robinhood", "note": "executor",
               "report_date": row["report_date"].isoformat() if isinstance(row.get("report_date"), date)
               else row.get("report_date")}
        made = self.sb.table("trade_orders").insert(rec).execute().data[0]
        full = {"status": "APPROVED", **row, "id": made["id"]}
        self.orders[made["id"]] = full
        return full

    def update_order(self, oid: int, **kw) -> None:
        super().update_order(oid, **kw)
        rec = {}
        if kw.get("status") in PERSISTED_STATUSES:
            rec["status"] = kw["status"]
        if "broker_order_id" in kw:
            rec["broker_order_id"] = kw["broker_order_id"]
        if rec:
            rec["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.sb.table("trade_orders").update(rec).eq("id", oid).execute()

    def add_event(self, d: date, order: dict, kind: str, ok: bool, broker_order_id=None, detail=None) -> None:
        super().add_event(d, order, kind, ok, broker_order_id, detail)
        if order.get("id") is not None:
            self.sb.table("exec_events").insert(self.events[-1]).execute()


async def _pages(rh, tool: str, args: dict, key: str, max_pages: int = 5) -> dict:
    items, cursor = [], None
    for _ in range(max_pages):
        res = await rh.call(tool, {**args, **({"cursor": cursor} if cursor else {})})
        data = res.get("data", res) if isinstance(res, dict) else {}
        items += data.get(key) or []
        cursor = data.get("next")
        if not cursor:
            break
    return {"data": {key: items}}


async def run_live(sb, today: date, cal: list, now: datetime) -> dict:
    from rh_client import open_robinhood
    loop = asyncio.get_running_loop()
    async with open_robinhood(interactive=False, handle=TradingRobinhood) as rh:
        accts = (await rh.call("get_accounts")).get("data", {}).get("accounts", [])
        if not any(a.get("account_number") == ACCOUNT and a.get("agentic_allowed") for a in accts):
            raise RuntimeError("agentic account not visible as tradable")
        if not await rh.approve_gate():
            raise RuntimeError("Robinhood's approval queue is OFF (human_must_approve_trades is not true): "
                               "nothing sent. Turn approvals back on in the app.")

        ledger = SupabaseLedger(sb=sb, plan_id=f"executor-{today.isoformat()}")
        ledger, source, links = snapshot_ledger(sb, today, live=True, ledger=ledger)
        plan_tickers = {o["ticker"] for o in ledger.orders.values() if o["plan_date"] == today.isoformat()}

        since10 = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        since30 = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        portfolio = await rh.call("get_portfolio", {"account_number": ACCOUNT})
        positions = await rh.call("get_equity_positions", {"account_number": ACCOUNT})
        orders = await rh.call("get_equity_orders", {"account_number": ACCOUNT, "created_at_gte": since10})
        advanced = await _pages(rh, "get_advanced_orders", {"account_number": ACCOUNT, "created_at_gte": since30},
                                "advanced_orders")
        approvals = await _pages(rh, "get_trade_approvals", {"account_number": ACCOUNT}, "approvals")
        held = {p["symbol"].upper() for p in (positions.get("data", positions).get("positions") or [])
                if float(p.get("quantity") or 0) > 0}
        syms = sorted(held | plan_tickers)
        quotes = [await rh.call("get_equity_quotes", {"symbols": syms[i:i + 20]}) for i in range(0, len(syms), 20)]

        def call(name, args):
            return asyncio.run_coroutine_threadsafe(rh.call(name, args), loop).result(timeout=60)

        broker = LiveRobinhoodBroker(portfolio, positions, orders, quotes, links, cal[max(0, len(cal) - 4)],
                                     advanced=advanced, approvals=approvals,
                                     cids=[f"wi-{i}" for i in ledger.orders], call=call)
        # Bring the ledger's broker ids up to date first (approved approvals, filled OCO legs).
        for cid, bid in broker.relinks().items():
            oid = _oid(cid)
            if oid in ledger.orders and ledger.orders[oid].get("broker_order_id") != bid:
                ledger.update_order(oid, broker_order_id=bid)

        ex = Executor(broker, ledger, cal)
        res = await asyncio.to_thread(ex.pre_open, today, now)

    refusals, seen = [], set()
    for (_, oid, t, intent, reasons) in ledger.refusals:
        k = (oid, tuple(reasons))
        if k not in seen:
            seen.add(k)
            refusals.append({"order_id": oid, "ticker": t, "intent": intent, "reasons": reasons})
    return {"run_date": today.isoformat(), "run_at": now.isoformat(), "status": res["status"],
            "mandate_source": source, "sent": broker.sent, "refusals": refusals,
            "problems": res.get("problems") or [], "alerts": ledger.alerts}


def run() -> int:
    if not live_switch_on():
        print(f"LIVE SWITCH OFF: {SWITCH} is not 1. Nothing sent (the dry run covers today).")
        return 0
    from config import load_config
    from loader import get_client
    cfg = load_config()
    sb = get_client(cfg.supabase_url, cfg.supabase_key)
    today = today_ny()
    cal = calendar(today)
    if today not in cal:
        print("NYSE closed today.")
        return 0
    now = datetime.now(timezone.utc)
    out = asyncio.run(run_live(sb, today, cal, now))

    sent_ok = [s for s in out["sent"] if "error" not in s]
    print(f"LIVE {today} · {out['status']} · mandate {out['mandate_source']} · sent {len(sent_ok)} "
          f"to the approval queue · errors {len(out['sent']) - len(sent_ok)}")
    for p in out["problems"]:
        print(f"  HALTED: {p}")
    for s in out["sent"]:
        a = s["args"]
        px = "".join(f" {k.split('_')[0]} {a[k]}" for k in ("limit_price", "stop_price", "stop_loss_stop_price",
                                                            "take_profit_limit_price") if a.get(k))
        what = f"{s['tool']} {a.get('side', '')} {a.get('quantity', '')} {a.get('symbol', '')}{px}".strip()
        print(f"  {'ERROR ' + s['error'] if 'error' in s else 'SENT  '} {what}")
    for r in out["refusals"]:
        print(f"  refused  {r['intent']:<12} {r['ticker']:<6} {', '.join(r['reasons'])}")
    if not out["sent"] and not out["refusals"] and out["status"] == "OK":
        print("  nothing to do")
    if out["mandate_source"] == "NONE":
        print("  no mandate on the dashboard today: entries refused, exits and stops still sent")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{today.isoformat()}-{datetime.now(NY):%H%M}.json").write_text(json.dumps(out, indent=1, default=str),
                                                                           encoding="utf-8")
    return 0


def main() -> int:
    try:
        return run()
    except Exception as e:
        root = e
        while isinstance(root, BaseExceptionGroup) and root.exceptions:
            root = root.exceptions[0]
        print(f"FAILED: {type(root).__name__}: {root}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
