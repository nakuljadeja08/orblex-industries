"""
jobs.py — the scheduled trading jobs as plain Python, for GitHub Actions (trading_jobs.yml),
dispatched on time by Supabase pg_cron (automation_schema.sql). No Claude, no PC.

  morning_plan       wait for today's pipeline (risk stage done), read the account, sync fills,
                     build today's order plan → email "plan ready"
  dryrun             the executor dry run (run_dryrun.py); with the phase 5 switch on
                     (EXECUTOR_LIVE=1) the LIVE executor instead (run_executor.py), which sends
                     orders to Robinhood's in-app approval queue
  executor_pass      with the switch on, a later executor pass: entries approved after 9:25 and
                     protective stops on today's fills. Off: records SKIPPED and reads nothing
  fill_sync          read orders/positions, sync fills, alert once per day on a position with
                     no covering stop
  eod_review        read the account, sync, compare the dry run, compute the daily review →
                     daily_reviews (admin-only) → email summary
  holdings_snapshot  the MAIN account's weights for the LONG TERM tab (Saturdays)

Every job records a job_runs row (RUNNING → OK / WARN / FAILED / SKIPPED) and emails on
anything that needs a human. Robinhood is read through rh_client (read-only tools only),
with the sign-in kept encrypted in Supabase (RH_TOKEN_STORE=supabase). Nothing here places,
cancels or approves an order, except run_executor.py when you have turned the switch on, and
even then every order waits for your approval in the Robinhood app.

Usage:  python jobs.py <job> [--dry-run]     (--dry-run: no DB writes except job_runs, no email)
"""

import argparse
import asyncio
import json
import logging
import os
import statistics
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass
logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("jobs")

NY = ZoneInfo("America/New_York")
from accounts import AGENTIC_ACCOUNT as ACCOUNT, MAIN_ACCOUNT   # noqa: E402  (from the environment)
DASHBOARD = os.environ.get("DASHBOARD_URL", "")
PIPELINE_WAIT_MIN = 35
LIVE = {"queued", "confirmed", "unconfirmed", "partially_filled"}


def today_ny() -> date:
    return datetime.now(NY).date()


def market_open(d: date) -> bool:
    import pandas_market_calendars as mcal
    return not mcal.get_calendar("NYSE").schedule(start_date=d, end_date=d).empty


def folder(d: date) -> Path:
    p = Path(".trading") / d.isoformat()
    p.mkdir(parents=True, exist_ok=True)
    return p


def py(*args: str, ok_if: str = "") -> str:
    """Run one of the project's CLIs; return its output; raise on failure unless ok_if is in it."""
    r = subprocess.run([sys.executable, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 and not (ok_if and ok_if in out):
        raise RuntimeError(f"{' '.join(args[:2])} failed ({r.returncode}): {out.strip()[-600:]}")
    return out


# ── Robinhood reads (read-only, through rh_client) ─────────────────────────────

async def _fetch(account: str, what: set, quote_symbols: set | None = None) -> dict:
    from rh_client import open_robinhood
    out = {}
    async with open_robinhood(interactive=False) as rh:
        if "portfolio" in what:
            out["portfolio"] = await rh.call("get_portfolio", {"account_number": account})
        if "positions" in what:
            out["positions"] = await rh.call("get_equity_positions", {"account_number": account})
        if "orders" in what:
            since = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            out["orders"] = await rh.call("get_equity_orders", {"account_number": account, "created_at_gte": since})
        syms = set(quote_symbols or set())
        if "quotes_held" in what:
            pos = out.get("positions") or {}
            syms |= {p["symbol"].upper() for p in (pos.get("data", pos).get("positions") or [])
                     if float(p.get("quantity") or 0) > 0 and not p["symbol"].endswith("^")}   # "^" = inactive
        if syms:
            s = sorted(syms)
            out["quotes"] = [await rh.call("get_equity_quotes", {"symbols": s[i:i + 20]}) for i in range(0, len(s), 20)]
    return out


def fetch(account: str, what: set, quote_symbols: set | None = None) -> dict:
    return asyncio.run(_fetch(account, what, quote_symbols))


def save(d: date, name: str, obj) -> str:
    p = folder(d) / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def _data(x):
    return x.get("data", x) if isinstance(x, dict) else {}


# ── job_runs ───────────────────────────────────────────────────────────────────

class Run:
    def __init__(self, sb, job: str, dry: bool):
        self.sb, self.job, self.dry = sb, job, dry
        url = os.environ.get("GITHUB_SERVER_URL")
        gh = f"{url}/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ.get('GITHUB_RUN_ID')}" if url else None
        try:
            self.id = sb.table("job_runs").insert({"job": job, "run_date": today_ny().isoformat(),
                                                   "gh_run_url": gh}).execute().data[0]["id"]
        except Exception as e:                    # never let bookkeeping stop a trading job
            log.warning(f"  job_runs not recorded ({e}); is automation_schema.sql applied?")
            self.id = None

    def finish(self, status: str, summary: str, details: dict | None = None) -> None:
        if self.id is None:
            return
        self.sb.table("job_runs").update({"status": status, "summary": summary[:2000], "details": details or {},
                                          "finished_at": datetime.now(timezone.utc).isoformat()}).eq("id", self.id).execute()


def email(dry: bool, subject: str, body: str) -> None:
    if dry:
        log.info(f"[dry-run] would email: {subject}\n{body}")
        return
    from notify import send
    send(subject, body + (f"\n\nDashboard: {DASHBOARD}" if DASHBOARD else ""))


# ── jobs ───────────────────────────────────────────────────────────────────────

def wait_for_pipeline(sb, d: date, minutes: int) -> str | None:
    """Today's daily swing run id once its risk stage has saved, polling up to `minutes`."""
    deadline = time.time() + minutes * 60
    while True:
        r = (sb.table("agent_reports").select("run_id").eq("trading_mode", "swing").eq("report_date", d.isoformat())
             .not_.like("run_id", "longterm-%").order("created_at", desc=True).limit(1).execute().data)
        if r:
            run_id = r[0]["run_id"]
            if sb.table("risk_assessments").select("run_id").eq("analyst_run_id", run_id).limit(1).execute().data:
                return run_id
        if time.time() >= deadline:
            return None
        time.sleep(60)


def job_morning_plan(sb, d: date, dry: bool) -> tuple[str, str, dict]:
    run_id = wait_for_pipeline(sb, d, PIPELINE_WAIT_MIN)
    late = run_id is None
    acct = fetch(ACCOUNT, {"portfolio", "positions", "orders"})
    pf, pos, orders = (save(d, n, acct[k]) for n, k in
                       (("portfolio.json", "portfolio"), ("positions.json", "positions"), ("orders.json", "orders")))
    sync = py("sync_fills.py", "--orders", orders, *(["--dry-run"] if dry else []))
    sym_out = py("run_trade_plan.py", "symbols", "--positions", pos)
    symbols = json.loads(next(ln for ln in reversed(sym_out.splitlines()) if ln.strip().startswith("[")))
    quotes = fetch(ACCOUNT, set(), set(symbols)).get("quotes") or [{"data": {"results": []}}]
    qfiles = [save(d, f"quotes-{i + 1}.json", q) for i, q in enumerate(quotes)]
    snap = str(folder(d) / "snapshot.json")
    py("broker_snapshot.py", "--portfolio", pf, "--positions", pos, "--orders", orders, "--quotes", *qfiles, "--out", snap)
    plan = py("run_trade_plan.py", "plan", "--account", snap, "--quotes", *qfiles, *(["--dry-run"] if dry else []),
              ok_if="already exists")
    exists = "already exists" in plan
    todays = sb.table("trade_orders").select("ticker,side,intent,order_type,qty,limit_price,stop_price,status,guard_reasons") \
        .eq("plan_date", d.isoformat()).order("id").execute().data or []
    proposed = [o for o in todays if o["status"] == "PROPOSED"]
    blocked = [o for o in todays if o["status"] == "BLOCKED"]
    lines = [f"{o['side']} {o['qty']:g} {o['ticker']} {o['intent']} {o['order_type']}"
             + (f" limit {o['limit_price']}" if o.get("limit_price") else "") + (f" stop {o['stop_price']}" if o.get("stop_price") else "")
             for o in proposed]
    lines += [f"BLOCKED {o['ticker']} {o['intent']}: {', '.join(o.get('guard_reasons') or [])}" for o in blocked]
    bad = [o["ticker"] for o in blocked if "BAD_PRICE_DATA" in (o.get("guard_reasons") or [])]
    head = ("PIPELINE LATE: no finished swing run by now; the plan used yesterday's data. " if late else "") + \
           ("A plan for today already existed; not rebuilt. " if exists else "")
    summary = f"{head}{len(proposed)} to approve, {len(blocked)} blocked" + (f" ({len(bad)} BAD_PRICE_DATA)" if bad else "")
    # A real mandate each day is what proves that path before the live switch: the dry run
    # otherwise assumes one, and live entries need one.
    no_mandate = any(o["intent"] == "ENTRY" for o in proposed) and not (
        sb.table("trading_mandates").select("mandate_date").eq("mandate_date", d.isoformat()).limit(1).execute().data)
    email(dry, f"Swing plan: {len(proposed)} to approve" + (" · PIPELINE LATE" if late else ""),
          summary + "\n\n" + ("\n".join(lines) or "Nothing proposed today.")
          + ("\n\nNo mandate set for today: approve one on TRADE PLANS." if no_mandate else "")
          + "\n\nApprove on TRADE PLANS, then place each order exactly as its ticket says.")
    status = "WARN" if late or bad else "OK"
    return status, summary, {"pipeline_run": run_id, "proposed": lines, "sync": sync[-1500:]}


def job_dryrun(sb, d: date, dry: bool) -> tuple[str, str, dict]:
    # Link orders placed by hand since the last sync first, or the dry run sees them as untracked.
    acct = fetch(ACCOUNT, {"orders"})
    py("sync_fills.py", "--orders", save(d, f"orders-{datetime.now(NY):%H%M}.json", acct["orders"]),
       *(["--dry-run"] if dry else []))
    if live_executor_on() and not dry:
        return job_live_executor(sb, d)
    out = py("run_dryrun.py", *(["--no-save"] if dry else []))
    status = "WARN" if "HALTED" in out or "WOULD HALT" in out else ("FAILED" if out.startswith("FAILED") else "OK")
    first = out.strip().splitlines()[0] if out.strip() else "no output"
    if status != "OK":
        email(dry, "Executor dry run would HALT" if status == "WARN" else "Executor dry run failed", out[-2500:])
    return status, first, {"output": out[-4000:]}


def live_executor_on() -> bool:
    """Phase 5 switch: the repository variable EXECUTOR_LIVE=1. Off (the default) = dry run."""
    return os.environ.get("EXECUTOR_LIVE", "").strip() == "1"


def job_live_executor(sb, d: date) -> tuple[str, str, dict]:
    """The executor sends today's orders to Robinhood's approval queue (run_executor.py), then
    sync_fills links what Robinhood already shows. Emails what is waiting for your tap."""
    out = py("run_executor.py")
    acct = fetch(ACCOUNT, {"orders"})
    py("sync_fills.py", "--orders", save(d, f"orders-{datetime.now(NY):%H%M}-after.json", acct["orders"]))
    first = out.strip().splitlines()[0] if out.strip() else "no output"
    failed = out.startswith("FAILED")
    status = "FAILED" if failed else ("WARN" if "HALTED" in out or "ERROR" in out else "OK")
    sent = sum(1 for ln in out.splitlines() if ln.strip().startswith("SENT"))
    if failed or status == "WARN":
        email(False, "Executor: needs a look" if not failed else "Executor failed", out[-2500:])
    elif sent:
        email(False, f"Approve in Robinhood: {sent} order(s) waiting",
              out[-2500:] + "\n\nOpen the Robinhood app and approve or decline each one. "
                            "Anything you ignore expires on its own.")
    return status, first, {"output": out[-4000:], "mode": "live"}


def job_executor_pass(sb, d: date, dry: bool) -> tuple[str, str, dict]:
    """A later pass, so an approval or mandate after 9:25 still counts and a position filled
    during the day gets its protective OCO that day (each order is deduplicated by its ref_id)."""
    if dry or not live_executor_on():
        return "SKIPPED", "the live executor is off", {}
    acct = fetch(ACCOUNT, {"orders"})
    py("sync_fills.py", "--orders", save(d, f"orders-{datetime.now(NY):%H%M}.json", acct["orders"]))
    return job_live_executor(sb, d)


def unprotected_positions(positions: dict, orders: dict) -> list[dict]:
    """Held positions, including today's fills, without a working SELL stop covering the shares."""
    stops: dict = {}
    for o in (_data(orders).get("orders") or []):
        if (o.get("state") or "").lower() in LIVE and (o.get("side") or "").lower() == "sell" and \
                ((o.get("trigger") or "").lower() == "stop" or (o.get("type") or "").lower() in ("stop_market", "stop_limit")):
            t = (o.get("symbol") or "").upper()
            stops[t] = stops.get(t, 0.0) + float(o.get("quantity") or 0)
    out = []
    for p in (_data(positions).get("positions") or []):
        q = float(p.get("quantity") or 0)
        t = (p.get("symbol") or "").upper()
        if q > 0 and stops.get(t, 0.0) < q - 1e-9:
            out.append({"ticker": t, "qty": q, "covered": stops.get(t, 0.0)})
    return out


def plan_stops(sb, tickers: list[str]) -> dict:
    """{ticker: plan stop} from the latest filled entry, so a STOP MISSING alert can name the price."""
    if not tickers:
        return {}
    rows = (sb.table("trade_orders").select("ticker,stop_price").in_("ticker", tickers).eq("intent", "ENTRY")
            .in_("status", ["FILLED", "PARTIAL"]).order("created_at").execute().data or [])
    return {r["ticker"]: r["stop_price"] for r in rows}


def job_fill_sync(sb, d: date, dry: bool) -> tuple[str, str, dict]:
    acct = fetch(ACCOUNT, {"positions", "orders"})
    hhmm = datetime.now(NY).strftime("%H%M")
    orders = save(d, f"orders-{hhmm}.json", acct["orders"])
    save(d, f"positions-{hhmm}.json", acct["positions"])
    sync = py("sync_fills.py", "--orders", orders, *(["--dry-run"] if dry else []))
    bad = unprotected_positions(acct["positions"], acct["orders"])
    try:
        earlier = sb.table("job_runs").select("details").eq("job", "fill_sync").eq("run_date", d.isoformat()) \
            .neq("status", "RUNNING").execute().data or []
    except Exception:
        earlier = []                              # no history yet: alert (better twice than never)
    already = {t for r in earlier for t in ((r.get("details") or {}).get("alerted") or [])}
    new = [b for b in bad if b["ticker"] not in already]
    if new:
        stop_at = plan_stops(sb, [b["ticker"] for b in new])
        email(dry, "STOP MISSING: " + ", ".join(b["ticker"] for b in new),
              "\n".join(f"{b['ticker']}: {b['qty']:g} sh held, stops cover {b['covered']:g}"
                        + (f", plan stop {stop_at[b['ticker']]}" if stop_at.get(b["ticker"]) else "") for b in new)
              + ("\n\nThe executor's next pass sends its OCO to your approval queue." if live_executor_on()
                 else "\n\nPlace the plan stop in the app (or approve its PROTECT_STOP on TRADE PLANS). "
                      "Same-day stops count as day trades only if they fire."))
    untracked = [ln for ln in sync.splitlines() if "UNTRACKED" in ln]
    if untracked:
        email(dry, "Untracked broker order", "\n".join(untracked))
    summary = (f"{len(bad)} unprotected" if bad else "all protected") + (f", {len(untracked)} untracked" if untracked else "")
    return ("WARN" if bad or untracked else "OK"), summary, \
        {"alerted": sorted(already | {b["ticker"] for b in new}), "unprotected": bad, "sync": sync[-1500:]}


def _et(ts) -> datetime | None:
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(NY) if ts else None


def build_review(d: date, orders_today: list, fills: list, broker_orders: list, portfolio: dict,
                 positions: dict, report_at, dry_cmp: str, unprotected: list) -> dict:
    """Pure: the daily review the Claude task used to write by hand (EXECUTOR_DESIGN §… test run)."""
    by_broker = {o["id"]: o for o in broker_orders}
    lat = {"plan_to_approve": [], "approve_to_place": [], "place_to_fill": []}
    rows = []
    plan_at = min((_et(o["created_at"]) for o in orders_today), default=None)
    for o in orders_today:
        b = by_broker.get(o.get("broker_order_id")) or {}
        ap, pl = _et(o.get("approved_at")), _et(b.get("created_at"))
        fl = _et(b.get("last_transaction_at")) if float(b.get("cumulative_quantity") or 0) > 0 else None
        if ap and plan_at:
            lat["plan_to_approve"].append((ap - _et(o["created_at"])).total_seconds() / 60)
        if ap and pl:
            lat["approve_to_place"].append((pl - ap).total_seconds() / 60)
        if pl and fl:
            lat["place_to_fill"].append((fl - pl).total_seconds() / 60)
        rows.append({"id": o["id"], "ticker": o["ticker"], "intent": o["intent"], "status": o["status"],
                     "limit": o.get("limit_price"), "stop": o.get("stop_price"),
                     "placed_before_approval": bool(ap and pl and pl < ap),
                     "fill_price": float(b["average_price"]) if b.get("average_price") else None})
    counts = {}
    for o in orders_today:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
    reasons = {}
    for o in orders_today:
        for r in o.get("guard_reasons") or []:
            reasons[r] = reasons.get(r, 0) + 1
    pf = _data(portfolio)
    pos = [{"ticker": p["symbol"], "qty": float(p["quantity"]), "avg_cost": float(p.get("average_buy_price") or 0)}
           for p in (_data(positions).get("positions") or []) if float(p.get("quantity") or 0) > 0]
    med = {k: (round(statistics.median(v), 1) if v else None) for k, v in lat.items()}
    issues = []
    if report_at is None:
        issues.append("no swing report today")
    elif plan_at and report_at > plan_at:
        issues.append("plan built before the pipeline finished")
    if any(r["placed_before_approval"] for r in rows):
        issues.append("an order was placed before its approval")
    if unprotected:
        issues.append(f"{len(unprotected)} position(s) without a stop")
    if reasons.get("BAD_PRICE_DATA"):
        issues.append(f"{reasons['BAD_PRICE_DATA']} entries blocked by BAD_PRICE_DATA")
    return {
        "date": d.isoformat(),
        "pipeline": {"report_at": report_at.strftime("%H:%M") if report_at else None},
        "plan": {"created_at": plan_at.strftime("%H:%M") if plan_at else None, "counts": counts, "blocked_reasons": reasons},
        "latency_median_min": med,
        "orders": rows,
        "fills": [{"ticker": f["ticker"], "side": f["side"], "qty": float(f["qty"]), "price": float(f["price"])} for f in fills],
        "safety": {"unprotected": unprotected},
        "account": {"total_value": float(pf.get("total_value") or 0), "cash": float(pf.get("cash") or 0), "positions": pos},
        "dryrun_vs_actual": dry_cmp,
        "issues": issues,
        "biggest_issue": issues[0] if issues else "none",
    }


def job_eod_review(sb, d: date, dry: bool) -> tuple[str, str, dict]:
    acct = fetch(ACCOUNT, {"portfolio", "positions", "orders"})
    orders = save(d, "eod-orders.json", acct["orders"])
    save(d, "eod-portfolio.json", acct["portfolio"])
    save(d, "eod-positions.json", acct["positions"])
    py("sync_fills.py", "--orders", orders, *(["--dry-run"] if dry else []))
    try:
        cmp_out = py("compare_dryrun.py", "--orders", orders)
    except RuntimeError as e:
        cmp_out = f"n/a ({str(e)[:200]})"
    todays = sb.table("trade_orders").select("*").eq("plan_date", d.isoformat()).order("id").execute().data or []
    fills = sb.table("trade_fills").select("*").eq("trade_date", d.isoformat()).execute().data or []
    rep = (sb.table("agent_reports").select("created_at").eq("trading_mode", "swing").eq("report_date", d.isoformat())
           .not_.like("run_id", "longterm-%").order("created_at").limit(1).execute().data)
    unprot = unprotected_positions(acct["positions"], acct["orders"])
    review = build_review(d, todays, fills, _data(acct["orders"]).get("orders") or [], acct["portfolio"],
                          acct["positions"], _et(rep[0]["created_at"]) if rep else None, cmp_out.strip()[-1500:], unprot)
    summary = f"{review['plan']['counts']} · biggest issue: {review['biggest_issue']}"   # no account value: job_runs
    if not dry:
        sb.table("daily_reviews").upsert({"run_date": d.isoformat(), "review": review, "summary": summary},
                                         on_conflict="run_date").execute()
    email(dry, f"Daily review {d}: {review['biggest_issue']}",
          json.dumps({k: review[k] for k in ("plan", "latency_median_min", "safety", "issues")}, indent=1)
          + "\n\nDry run vs actual:\n" + cmp_out[-1500:])
    return ("WARN" if review["issues"] else "OK"), summary, {"issues": review["issues"]}


def job_holdings_snapshot(sb, d: date, dry: bool) -> tuple[str, str, dict]:
    acct = fetch(MAIN_ACCOUNT, {"portfolio", "positions", "quotes_held"})
    sub = Path(".trading") / "longterm" / d.isoformat()
    sub.mkdir(parents=True, exist_ok=True)
    files = {}
    for k in ("portfolio", "positions"):
        files[k] = str(sub / f"{k}.json")
        Path(files[k]).write_text(json.dumps(acct[k]), encoding="utf-8")
    qf = []
    for i, q in enumerate(acct.get("quotes") or [{"data": {"results": []}}]):
        p = sub / f"quotes-{i + 1}.json"
        p.write_text(json.dumps(q), encoding="utf-8")
        qf.append(str(p))
    out = py("save_holdings.py", "--portfolio", files["portfolio"], "--positions", files["positions"],
             "--quotes", *qf, *(["--dry-run"] if dry else []))
    first = next((ln for ln in out.splitlines() if "holding(s)" in ln), "saved")
    return ("WARN" if "NO QUOTE" in first else "OK"), first, {}


JOBS = {"morning_plan": job_morning_plan, "dryrun": job_dryrun, "executor_pass": job_executor_pass,
        "fill_sync": job_fill_sync, "eod_review": job_eod_review, "holdings_snapshot": job_holdings_snapshot}
WEEKDAY_ONLY = {"morning_plan", "dryrun", "executor_pass", "fill_sync", "eod_review"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Orblex Industries scheduled trading jobs")
    ap.add_argument("job", choices=sorted(JOBS))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    from config import load_config
    from loader import get_client
    cfg = load_config()
    sb = get_client(cfg.supabase_url, cfg.supabase_key)
    d = today_ny()
    run = Run(sb, args.job, args.dry_run)
    try:
        if args.job in WEEKDAY_ONLY and not market_open(d):
            run.finish("SKIPPED", "NYSE closed")
            log.info("NYSE closed: skipped")
            return 0
        status, summary, details = JOBS[args.job](sb, d, args.dry_run)
        run.finish(status, summary, details)
        log.info(f"{args.job}: {status} · {summary}")
        return 0
    except Exception as e:
        root = e
        while isinstance(root, BaseExceptionGroup) and root.exceptions:
            root = root.exceptions[0]
        msg = f"{type(root).__name__}: {root}"
        run.finish("FAILED", msg[:2000])
        email(args.dry_run, f"Job FAILED: {args.job}", msg)
        log.error(f"{args.job}: FAILED · {msg}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
