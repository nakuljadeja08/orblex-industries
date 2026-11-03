"""
run_dryrun.py — phase 4 (EXECUTOR_DESIGN.md §5): run the real executor code against the LIVE
Robinhood account in dry-run, once each morning before the open.

  read     rh_client (read-only tools only; your stored sign-in): accounts, portfolio,
           positions, recent orders, quotes for held + today's plan tickers
  ledger   an in-memory SNAPSHOT of the Supabase ledger (trade_orders, trade_fills,
           exec_events, today's mandate, the kill switch). Today's PROPOSED/APPROVED
           entries are treated as approved. With no dashboard mandate, an ASSUMED one is
           used (those tickers, 2 entries, their summed risk) and the run says so.
  run      executor.pre_open twice through DryRunBroker: submit()/cancel() only record
  write    .trading/dryrun/YYYY-MM-DD-HHMM.json and an exec_dryrun row. The executor's own
           writes stay in the snapshot: trade_orders, trade_fills and exec_events are
           NEVER written, and nothing is sent to Robinhood.

Usage:
  .venv\\Scripts\\python run_dryrun.py [--no-save]
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from exec_limits import ENTRY, Mandate
from exec_state import last_sessions
from executor import Executor, MemoryLedger
from robinhood_broker import DryRunBroker, RobinhoodBroker

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass
logging.basicConfig(level=logging.WARNING, format="%(message)s")

NY = ZoneInfo("America/New_York")
from accounts import AGENTIC_ACCOUNT as ACCOUNT   # noqa: E402  (from the environment)
OUT = Path(".trading/dryrun")


def today_ny() -> date:
    return datetime.now(NY).date()


def calendar(today: date) -> list[date]:
    return last_sessions(today, 40)


# ── ledger snapshot ────────────────────────────────────────────────────────────

def snapshot_ledger(sb, today: date, live: bool = False, ledger: MemoryLedger | None = None
                    ) -> tuple[MemoryLedger, str, dict]:
    """live=True (phase 5): only entries you APPROVED on the dashboard, your dashboard mandate
    or none at all (entries are then refused), and nothing ASSUMED."""
    since = (today - timedelta(days=45)).isoformat()
    orders = sb.table("trade_orders").select("*").gte("plan_date", since).order("id").execute().data or []
    fills = sb.table("trade_fills").select("*").order("filled_at").execute().data or []
    events = sb.table("exec_events").select("*").gte("trade_date", since).execute().data or []
    m = sb.table("trading_mandates").select("*").eq("mandate_date", today.isoformat()).limit(1).execute().data
    ctl = (sb.table("trading_controls").select("*").eq("id", 1).execute().data or [{}])[0]

    L = ledger if ledger is not None else MemoryLedger()
    for o in orders:
        row = dict(o)
        row["report_date"] = date.fromisoformat(str(o["report_date"])[:10]) if o.get("report_date") else None
        row["plan_date"] = str(o["plan_date"])[:10]
        L.orders[o["id"]] = row
    todays = [o for o in L.orders.values() if o["plan_date"] == today.isoformat() and o["intent"] == ENTRY
              and o["status"] in (("APPROVED",) if live else ("PROPOSED", "APPROVED"))]
    for rank, o in enumerate(todays):
        if live:
            o.update(rank=rank)
        else:
            o.update(status="APPROVED", broker_order_id=None, rank=rank)
    for i, f in enumerate(fills, start=1):
        L.fills.append({"order_id": f.get("order_id"), "ticker": f["ticker"], "side": f["side"],
                        "qty": int(float(f["qty"])) if float(f["qty"]).is_integer() else float(f["qty"]),
                        "price": float(f["price"]), "trade_date": str(f["trade_date"])[:10], "seq": i,
                        "filled_at": str(f.get("filled_at") or "")})
        if f.get("broker_order_id"):
            L.synced.add(f["broker_order_id"])
    L.synced |= {o["broker_order_id"] for o in orders if o.get("broker_order_id") and o["status"] in ("FILLED", "PARTIAL")}
    L.events = [{**e, "trade_date": str(e["trade_date"])[:10]} for e in events]
    L.halted, L.halt_reason = bool(ctl.get("halted")), ctl.get("halt_reason")
    L.equity_peak = float(ctl["equity_peak"]) if ctl.get("equity_peak") else None
    L._next_id = (max(L.orders) if L.orders else 0) + 1_000_000          # executor-made rows never collide
    if m:
        L.mandates[today] = Mandate.from_row(m[0])
        source = "DASHBOARD"
    elif live:
        source = "NONE"
    else:
        close = datetime.combine(today, datetime.min.time(), tzinfo=NY).replace(hour=16).astimezone(timezone.utc)
        L.mandates[today] = Mandate(mandate_date=today, max_entries=2,
                                    max_total_risk_usd=round(sum(float(o.get("risk_dollars") or 0) for o in todays), 2),
                                    allowed_tickers=frozenset(o["ticker"] for o in todays), expires_at=close)
        source = "ASSUMED"
    links = {o["broker_order_id"]: o["id"] for o in orders if o.get("broker_order_id")}
    return L, source, links


# ── live reads ─────────────────────────────────────────────────────────────────

async def fetch_live(tickers: set) -> dict:
    from rh_client import open_robinhood
    async with open_robinhood(interactive=False) as rh:
        accts = (await rh.call("get_accounts")).get("data", {}).get("accounts", [])
        if not any(a.get("account_number") == ACCOUNT and a.get("agentic_allowed") for a in accts):
            raise RuntimeError("agentic account not visible as tradable")
        portfolio = await rh.call("get_portfolio", {"account_number": ACCOUNT})
        positions = await rh.call("get_equity_positions", {"account_number": ACCOUNT})
        since = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        orders = await rh.call("get_equity_orders", {"account_number": ACCOUNT, "created_at_gte": since})
        held = {p["symbol"].upper() for p in (positions.get("data", positions).get("positions") or [])
                if float(p.get("quantity") or 0) > 0}
        syms = sorted(held | tickers)
        quotes = []
        for i in range(0, len(syms), 20):
            quotes.append(await rh.call("get_equity_quotes", {"symbols": syms[i:i + 20]}))
    return {"portfolio": portfolio, "positions": positions, "orders": orders, "quotes": quotes}


def run(save: bool) -> int:
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

    ledger, source, links = snapshot_ledger(sb, today)
    plan_tickers = {o["ticker"] for o in ledger.orders.values() if o["plan_date"] == today.isoformat()}
    live = asyncio.run(fetch_live(plan_tickers))
    sessions_back = cal[max(0, len(cal) - 4)]
    real = RobinhoodBroker(live["portfolio"], live["positions"], live["orders"], live["quotes"], links, sessions_back)
    dry = DryRunBroker(real)
    ex = Executor(dry, ledger, cal)
    res = ex.pre_open(today, now)
    if res["status"] == "OK":
        ex.pre_open(today, now)                                   # the loop's next pass: must add nothing

    refusals = [{"order_id": oid, "ticker": t, "intent": intent, "reasons": reasons}
                for (_, oid, t, intent, reasons) in ledger.refusals]
    seen, uniq = set(), []
    for r in refusals:                                            # the second pass repeats refusals
        k = (r["order_id"], tuple(r["reasons"]))
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    out = {"run_date": today.isoformat(), "run_at": now.isoformat(), "status": res["status"],
           "mandate_source": source, "would": dry.would, "refusals": uniq,
           "problems": res.get("problems") or [], "alerts": ledger.alerts}

    acct = real.account()
    print(f"Dry run {today} · {res['status']} · mandate {source} · positions {sorted(acct.positions)} · "
          f"working orders {sum(1 for o in real.orders() if o.state == 'WORKING')}")
    for p in out["problems"]:
        print(f"  WOULD HALT: {p}")
    for w in dry.would:
        if w["action"] == "SUBMIT":
            px = f" limit {w['limit']}" if w.get("limit") else ""
            px += f" stop {w['stop']}" if w.get("stop") else ""
            px += f" target {w['take_profit']}" if w.get("take_profit") else ""
            print(f"  WOULD SUBMIT {w['side']} {w['qty']} {w['ticker']} {w['type']}{px} ({w['tif']})")
        else:
            print(f"  WOULD CANCEL {w['ticker']} {w['type']} ({w['broker_order_id']})")
    for r in uniq:
        print(f"  refused  {r['intent']:<12} {r['ticker']:<6} {', '.join(r['reasons'])}")
    if not dry.would and not uniq and res["status"] == "OK":
        print("  nothing to do")

    if save:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{today.isoformat()}-{datetime.now(NY):%H%M}.json").write_text(json.dumps(out, indent=1, default=str),
                                                                               encoding="utf-8")
        sb.table("exec_dryrun").insert({k: out[k] for k in ("run_date", "run_at", "status", "mandate_source", "would",
                                                            "refusals", "problems", "alerts")}).execute()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Executor dry run against the live Robinhood account")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    try:
        return run(save=not args.no_save)
    except Exception as e:
        root = e
        while isinstance(root, BaseExceptionGroup) and root.exceptions:
            root = root.exceptions[0]
        print(f"FAILED: {type(root).__name__}: {root}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
