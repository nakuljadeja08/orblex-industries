"""
run_trade_plan.py — builds, checks and records swing orders for the account.

Places no orders itself. The execution session (Claude with the Robinhood MCP)
reads the account, writes it to a snapshot JSON, and drives this CLI:

  plan        build today's orders from the latest swing reports        → trade_orders
  approve     record the user's yes/no on an order                     (APPROVED / REJECTED)
  check       re-run every guard right before placing, with a live quote;
              refuses any order not APPROVED. Exit code 0 = place it; anything
              else (2 = blocked, or an error) = do not place.
  placed      record the broker order id                               (PLACED)
  fill        record an execution                                       → trade_fills
  cancel      mark an order cancelled / expired
  halt/resume kill switch for new entries                               → trading_controls
  show        today's orders

Usage:
  python run_trade_plan.py plan --account snapshot.json [--dry-run]
  python run_trade_plan.py symbols --positions positions.json     # tickers to quote on Robinhood
  python run_trade_plan.py plan --account snapshot.json --quotes quotes.json
  python run_trade_plan.py plan --account snapshot.json --quotes quotes.json --replace   # rebuild today's plan
  python run_trade_plan.py plan --paper 5000 --dry-run
  python run_trade_plan.py approve 42 [--reject]
  python run_trade_plan.py check 42 --account snapshot.json --quote 227.10
  python run_trade_plan.py placed 42 --broker-order-id abc123
  python run_trade_plan.py fill 42 --qty 5 --price 227.05 [--time 2026-10-12T09:41:00-04:00]
  python run_trade_plan.py halt --reason "testing"   |   resume
  python run_trade_plan.py show

Account snapshot (all amounts USD, as_of must be today in New York for a live account):
  {"as_of": "2026-10-12T09:20:00-04:00", "source": "robinhood",
   "equity": 5000.0, "buying_power": 4800.0, "day_pnl": -12.5,
   "positions":   [{"ticker": "NVDA", "qty": 5, "avg_cost": 227.5, "last_price": 229.1}],
   "fills_today": [{"ticker": "NVDA", "side": "BUY", "qty": 5, "price": 227.5}],
   "open_orders": [{"id": "rh-1", "ticker": "NVDA", "side": "SELL", "type": "STOP", "qty": 5}]}
"""

import argparse
import json
import logging
import os
import sys
import uuid
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from config import load_config
from exec_state import load_exec_state
from loader import get_client
from trade_guard import (
    BUY, ENTRY, SELL, GuardContext, check_order, halt_reasons,
)
from price_checks import parse_quotes
from trade_plan import PROPOSED, build_plan, effective_level

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")
LIVE_STATUSES = ("PROPOSED", "APPROVED", "PLACED")
STATUSES = ("PROPOSED", "APPROVED", "REJECTED", "PLACED", "FILLED", "PARTIAL",
            "CANCELLED", "EXPIRED", "BLOCKED")


def today_ny() -> date:
    return datetime.now(NY).date()


def _d(x):
    return date.fromisoformat(str(x)[:10]) if x else None


# ── Account snapshot ────────────────────────────────────────────────────────

def load_snapshot(path: str | None, paper: float | None) -> dict:
    if paper is not None:
        return {"as_of": datetime.now(NY).isoformat(), "source": "paper", "equity": paper,
                "buying_power": paper, "day_pnl": 0.0, "positions": [], "fills_today": [],
                "open_orders": []}
    with open(path, encoding="utf-8") as f:
        snap = json.load(f)
    for k in ("as_of", "equity", "buying_power"):
        if k not in snap:
            raise SystemExit(f"Snapshot is missing '{k}'.")
    as_of = datetime.fromisoformat(snap["as_of"])
    if as_of.tzinfo is None:
        raise SystemExit("Snapshot as_of needs a timezone offset.")
    if snap.get("source", "robinhood") != "paper" and as_of.astimezone(NY).date() != today_ny():
        raise SystemExit(f"Snapshot as_of {snap['as_of']} is not today in New York — refresh it from the broker.")
    for k in ("positions", "fills_today", "open_orders"):
        snap.setdefault(k, [])
    return snap


# ── Ledger / pipeline reads ─────────────────────────────────────────────────

# A paper dry run may proceed without the ledger tables (trading_schema.sql not
# applied yet). Everything else fails closed: the same-day rules depend on them.
LEDGER_OPTIONAL = False


def _ledger_read(what: str, fn, default):
    try:
        return fn()
    except Exception as exc:
        if LEDGER_OPTIONAL:
            logger.warning(f"  {what} unreadable ({str(exc)[:80]}) — ignored for this paper dry run.")
            return default
        raise SystemExit(f"{what} unreadable ({exc}). Apply trading_schema.sql first.")


def _ledger_fills_today(sb, today: date) -> list[dict]:
    return _ledger_read("trade_fills", lambda: sb.table("trade_fills").select("ticker,side")
                        .eq("trade_date", today.isoformat()).execute().data or [], [])


def _managed_positions(sb, tickers: list[str]) -> dict:
    """Plan levels for positions we opened: the latest filled ENTRY per ticker."""
    if not tickers:
        return {}
    orders = (sb.table("trade_orders")
              .select("id,ticker,stop_price,target_price,horizon_days,report_run_id,status")
              .in_("ticker", tickers).eq("intent", ENTRY).in_("status", ["FILLED", "PARTIAL"])
              .order("created_at", desc=True).execute().data or [])
    out = {}
    for o in orders:
        if o["ticker"] in out:
            continue
        fills = (sb.table("trade_fills").select("trade_date").eq("order_id", o["id"])
                 .order("trade_date").limit(1).execute().data or [])
        if not fills:
            continue
        out[o["ticker"]] = {
            "order_id": o["id"], "opened_date": _d(fills[0]["trade_date"]),
            "stop_price": float(o["stop_price"]),
            "target_price": float(o["target_price"]) if o.get("target_price") else None,
            "horizon_days": o.get("horizon_days"), "report_run_id": o.get("report_run_id"),
        }
    return out


def _controls(sb) -> dict:
    rows = _ledger_read("trading_controls",
                        lambda: sb.table("trading_controls").select("*").eq("id", 1).execute().data, [])
    c = rows[0] if rows else {"halted": False}
    if os.getenv("TRADING_HALTED", "").strip().lower() == "true":
        c = {**c, "halted": True, "halt_reason": "TRADING_HALTED env var"}
    return c


def _approved_unplaced(sb, today: date, exclude_id: int | None) -> list[dict]:
    """
    Today's APPROVED orders not yet linked to a broker order. The broker does not
    know about them (nothing reserves buying power until they are placed), so the
    guard must: entries count like entries earlier in the batch, sells like
    working sell orders.
    """
    rows = _ledger_read("trade_orders", lambda: sb.table("trade_orders")
                        .select("id,ticker,side,intent,order_type,qty,limit_price,stop_price,broker_order_id")
                        .eq("plan_date", today.isoformat()).eq("status", "APPROVED")
                        .execute().data or [], [])
    out = []
    for r in rows:
        if r["id"] == exclude_id or r.get("broker_order_id"):
            continue
        out.append({**r, "qty": float(r["qty"]),
                    "limit_price": float(r["limit_price"]) if r.get("limit_price") is not None else None,
                    "stop_price": float(r["stop_price"]) if r.get("stop_price") is not None else None})
    return out


def build_context(sb, snap: dict, today: date, universe: set | None,
                  exclude_order_id: int | None = None) -> GuardContext:
    positions = {p["ticker"].upper(): p for p in snap["positions"] if float(p.get("qty") or 0) > 0}
    ledger = _ledger_fills_today(sb, today)
    fills = snap["fills_today"] + ledger
    ctrl = _controls(sb)
    pending = _approved_unplaced(sb, today, exclude_order_id)
    approved_entries = [p for p in pending if p["intent"] == ENTRY and p.get("limit_price") and p.get("stop_price")]
    approved_sells = [{"id": f"approved-{p['id']}", "ticker": p["ticker"], "side": SELL, "type": p["order_type"],
                       "qty": p["qty"]} for p in pending if p["side"] == SELL]
    exec_state = load_exec_state(sb, today)
    return GuardContext(
        today=today,
        equity=float(snap["equity"]),
        buying_power=float(snap["buying_power"]),
        positions=positions,
        bought_today={f["ticker"].upper() for f in fills if f["side"].upper() == BUY},
        sold_today={f["ticker"].upper() for f in fills if f["side"].upper() == SELL},
        open_orders=[{**o, "ticker": o["ticker"].upper(), "side": o["side"].upper()} for o in snap["open_orders"]]
                    + approved_sells,
        managed=_managed_positions(sb, list(positions)),
        universe=universe,
        halted=bool(ctrl.get("halted")),
        halt_reason=ctrl.get("halt_reason"),
        day_pnl=snap.get("day_pnl"),
        equity_peak=float(ctrl["equity_peak"]) if ctrl.get("equity_peak") else None,
        batch_entries=approved_entries,
        day_trades_5_sessions=exec_state.day_trades_5_sessions if exec_state.complete else None,
    )


def tradable_universe(cfg) -> set:
    """Tickers an order may be for: the core universe plus the curated sleeve
    pool (candidates.txt), which run_universe.py draws the daily sleeve from."""
    from instrument_policy import candidate_sectors
    return set(cfg.tickers) | set(candidate_sectors())


def _save_account_snapshot(sb, snap: dict) -> None:
    """Record the balance for the next pipeline's universe stage (run_universe.py).
    account_snapshots has no public read policy — the balance stays server-side."""
    try:
        as_of = datetime.fromisoformat(snap["as_of"])
        sb.table("account_snapshots").insert({
            "as_of": as_of.isoformat(), "snapshot_date": as_of.astimezone(NY).date().isoformat(),
            "source": snap.get("source", "robinhood"), "equity": snap["equity"],
            "buying_power": snap.get("buying_power"), "cash": snap.get("cash"),
            "positions": [{k: p.get(k) for k in ("ticker", "qty", "avg_cost", "last_price")}
                          for p in snap.get("positions", [])],
        }).execute()
    except Exception as exc:
        logger.warning(f"  account_snapshots not saved ({str(exc)[:80]}) — apply universe_schema.sql.")


def _latest_swing_run(sb) -> tuple[str | None, list[dict]]:
    r = (sb.table("agent_reports").select("run_id").eq("trading_mode", "swing")
         .order("created_at", desc=True).limit(1).execute().data)
    if not r:
        return None, []
    run_id = r[0]["run_id"]
    rows = (sb.table("agent_reports")
            .select("run_id,ticker,report_date,signal,signal_strength,setup_type,entry_price,"
                    "stop_price,target_price,horizon_days,risk_reward")
            .eq("run_id", run_id).execute().data or [])
    for x in rows:
        x["report_date"] = _d(x["report_date"])
    return run_id, rows


def _risk_inputs(sb, analyst_run_id: str) -> tuple[dict, str | None, str | None]:
    """({ticker: risk row}, risk manager level, regime floor) — all degrade to empty."""
    by_ticker, level, floor = {}, None, None
    try:
        ra = (sb.table("risk_assessments").select("run_id,market_risk_level")
              .eq("analyst_run_id", analyst_run_id).order("created_at", desc=True).limit(1).execute().data)
        if ra:
            level = ra[0]["market_risk_level"]
            rows = (sb.table("risk_ticker_details").select("ticker,risk_score,max_position_pct")
                    .eq("assessment_run_id", ra[0]["run_id"]).execute().data or [])
            by_ticker = {r["ticker"]: {**r, "max_position_pct": float(r["max_position_pct"])
                                       if r.get("max_position_pct") is not None else None} for r in rows}
        else:
            logger.warning("  No risk assessment for this analyst run — sizing without risk-manager caps.")
    except Exception as exc:
        logger.warning(f"  Risk lookup failed: {exc}")
    try:
        mr = sb.table("market_regimes").select("regime,risk_floor").order("created_at", desc=True).limit(1).execute().data
        floor = mr[0].get("risk_floor") if mr else None
    except Exception as exc:
        logger.warning(f"  Regime lookup failed: {exc}")
    return by_ticker, level, floor


def _load_quotes(paths) -> dict | None:
    """price_checks.parse_quotes over one or more saved get_equity_quotes responses."""
    if not paths:
        return None
    raws = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            raws.append(json.load(f))
    return parse_quotes(*raws)


def _stored_closes(sb, tickers: list[str]) -> dict:
    """{ticker: (close, date)} — the latest stock_prices bar the plan's levels were built from."""
    out = {}
    for t in tickers:
        r = sb.table("stock_prices").select("close,date").eq("ticker", t).order("date", desc=True).limit(1).execute().data
        if r:
            out[t] = (float(r[0]["close"]), str(r[0]["date"])[:10])
    return out


def _last_closes(sb, ctx: GuardContext, quotes: dict | None = None) -> dict:
    """Last close per held ticker for the close-based exit rules: Robinhood's
    official close when quoted, else the snapshot price, else stock_prices."""
    out = {t: q["official_close"] for t, q in (quotes or {}).items()
           if t in ctx.positions and q.get("official_close")}
    out.update({t: float(p["last_price"]) for t, p in ctx.positions.items()
                if p.get("last_price") and t not in out})
    for t in set(ctx.positions) - set(out):
        r = sb.table("stock_prices").select("close").eq("ticker", t).order("date", desc=True).limit(1).execute().data
        if r:
            out[t] = float(r[0]["close"])
    return out


def _carryover(sb, today: date, todays: dict) -> list[dict]:
    """Earlier unfilled entries still inside their window, unless today's report says SELL or re-plans."""
    rows = _ledger_read("trade_orders", lambda: sb.table("trade_orders").select("*").eq("intent", ENTRY)
                        .in_("status", list(LIVE_STATUSES)).lt("plan_date", today.isoformat())
                        .execute().data or [], [])
    out = []
    for o in rows:
        if todays.get(o["ticker"]) in ("SELL", "BUY"):
            continue
        out.append({"run_id": o["report_run_id"], "ticker": o["ticker"], "report_date": _d(o["report_date"]),
                    "signal_strength": o.get("signal_strength"), "setup_type": o.get("setup_type"),
                    "entry_price": o["limit_price"], "stop_price": o["stop_price"],
                    "target_price": o["target_price"], "horizon_days": o["horizon_days"],
                    "risk_reward": None, "_supersedes": o["id"]})
    return out


# ── Commands ────────────────────────────────────────────────────────────────

def _row(o: dict, plan_id: str, today: date, source: str) -> dict:
    return {
        "plan_id": plan_id, "plan_date": today.isoformat(), "ticker": o["ticker"], "side": o["side"],
        "intent": o["intent"], "order_type": o["order_type"], "time_in_force": o.get("time_in_force"),
        "qty": o["qty"], "limit_price": o.get("limit_price"), "stop_price": o.get("stop_price"),
        "target_price": o.get("target_price"), "horizon_days": o.get("horizon_days"),
        "risk_dollars": o.get("risk_dollars"), "report_run_id": o.get("report_run_id"),
        "report_date": o["report_date"].isoformat() if o.get("report_date") else None,
        "signal_strength": o.get("signal_strength"), "setup_type": o.get("setup_type"),
        "cancel_first": o.get("cancel_first") or [], "status": o["status"],
        "guard_reasons": o.get("guard_reasons") or [], "note": o.get("note"), "account_source": source,
    }


def cmd_plan(args, sb, cfg):
    global LEDGER_OPTIONAL
    LEDGER_OPTIONAL = bool(args.dry_run and args.paper is not None)
    today = today_ny()
    if not is_session(today):
        if not args.dry_run:
            raise SystemExit(f"{today} is not an NYSE session — build the plan on the trading day it is for.")
        logger.warning(f"  {today} is not an NYSE session — dry run only.")

    # One plan per day. A second run would duplicate every order, so it needs
    # --replace, which expires today's still-unapproved orders. Approved,
    # placed and filled orders are never replaced.
    existing = _ledger_read("trade_orders", lambda: sb.table("trade_orders").select("id,status")
                            .eq("plan_date", today.isoformat()).execute().data or [], [])
    pending = [o["id"] for o in existing if o["status"] in ("PROPOSED", "BLOCKED")]
    if existing and not args.replace and not args.dry_run:
        raise SystemExit(f"A plan for {today} already exists ({len(existing)} order(s)). "
                         f"Re-run with --replace to expire its {len(pending)} unapproved order(s) and rebuild.")

    snap = load_snapshot(args.account, args.paper)
    if snap.get("source") != "paper" and not args.dry_run:
        _save_account_snapshot(sb, snap)
    ctx = build_context(sb, snap, today, tradable_universe(cfg))

    run_id, reports = _latest_swing_run(sb)
    if not run_id:
        raise SystemExit("No swing-mode reports on record. Run: python run_agent.py --mode swing")
    rdate = reports[0]["report_date"] if reports else None
    fresh = rdate is not None and (rdate == today or (rdate < today and
                                                      _sessions_between(rdate, today) == 0))
    todays = {r["ticker"]: r["signal"] for r in reports} if fresh else {}
    if not fresh:
        logger.warning(f"  Latest swing run {run_id} is from {rdate} — too old to trade; exits only.")

    plans = [r for r in reports if r["signal"] == "BUY" and r.get("entry_price")] if fresh else []
    plans += _carryover(sb, today, todays)
    risk_by_ticker, rm_level, floor = _risk_inputs(sb, run_id)
    level = effective_level(rm_level, floor)

    # Robinhood price checks (price_checks.py). Required for a live account so a
    # morning run cannot skip them; paper and dry runs may go without.
    quotes = _load_quotes(args.quotes)
    if quotes is None and snap.get("source") != "paper" and not args.dry_run:
        raise SystemExit("A live plan needs --quotes (Robinhood get_equity_quotes for: "
                         "python run_trade_plan.py symbols).")
    missing = [p["ticker"] for p in plans if quotes is not None and p["ticker"] not in quotes]
    if missing:
        logger.warning(f"  No Robinhood quote for {', '.join(sorted(set(missing)))} — those entries are blocked.")
    stored = _stored_closes(sb, sorted({p["ticker"] for p in plans})) if quotes is not None else None
    result = build_plan(ctx, plans, todays, _last_closes(sb, ctx, quotes), risk_by_ticker, level,
                        quotes=quotes, stored_closes=stored,
                        executor_live=os.getenv("EXECUTOR_LIVE", "").strip() == "1")

    plan_id = f"tradeplan-{today.isoformat()}-{uuid.uuid4().hex[:6]}"
    orders = result["exits"] + result["entries"]
    _print_plan(snap, ctx, run_id, rdate, rm_level, floor, result)

    if args.dry_run:
        logger.info("\n[DRY RUN] Nothing written.")
        return
    if pending and args.replace:
        _set_status(sb, pending, "EXPIRED")
        logger.info(f"Expired {len(pending)} unapproved order(s) from the earlier plan.")
    if orders:
        sb.table("trade_orders").insert([_row(o, plan_id, today, snap.get("source", "robinhood"))
                                         for o in orders]).execute()
    superseded = [p["_supersedes"] for p in plans if p.get("_supersedes")]
    if superseded:
        _set_status(sb, superseded, "EXPIRED")
    peak = max(ctx.equity, ctx.equity_peak or 0)
    if snap.get("source") != "paper" and peak != ctx.equity_peak:
        sb.table("trading_controls").upsert({"id": 1, "equity_peak": peak,
                                             "updated_at": datetime.now(timezone.utc).isoformat()}).execute()
    logger.info(f"\nSaved {len(orders)} order(s) under {plan_id}. Nothing is placed until each is approved.")


def is_session(d: date) -> bool:
    try:
        import pandas_market_calendars as mcal
        return not mcal.get_calendar("NYSE").schedule(start_date=d, end_date=d).empty
    except Exception:
        return d.weekday() < 5


def _sessions_between(a: date, b: date) -> int:
    from swing_policy import sessions_missing
    return sessions_missing(a.isoformat(), b) or 0


def _print_plan(snap, ctx, run_id, rdate, rm_level, floor, result):
    halts = halt_reasons(ctx)
    logger.info("=" * 78)
    logger.info(f"Trade plan {ctx.today}  |  account {snap.get('source', 'robinhood')}  "
                f"equity ${ctx.equity:,.2f}  buying power ${ctx.buying_power:,.2f}")
    logger.info(f"Swing run {run_id} ({rdate})  |  risk level {result['level']} "
                f"(risk manager {rm_level or 'n/a'}, regime floor {floor or 'none'})")
    if halts:
        logger.info(f"NEW ENTRIES HALTED: {', '.join(halts)}"
                    + (f" — {ctx.halt_reason}" if ctx.halt_reason else ""))
    logger.info("=" * 78)
    for title, rows in (("EXITS", result["exits"]), ("ENTRIES", result["entries"])):
        logger.info(f"\n{title}")
        if not rows:
            logger.info("  none")
        for o in rows:
            px = (f"limit {o['limit_price']:.2f} stop {o['stop_price']:.2f} tgt {o['target_price']:.2f}"
                  if o["intent"] == ENTRY else
                  f"{o['order_type'].lower()}" + (f" @ {o['stop_price']:.2f}" if o["order_type"] == "STOP" else ""))
            risk = f" risk ${o['risk_dollars']:.0f}" if o.get("risk_dollars") else ""
            status = o["status"] if o["status"] == PROPOSED else f"BLOCKED {','.join(o['guard_reasons'])}"
            logger.info(f"  {o['side']:<4} {o['qty']:>4} {o['ticker']:<5} {o['intent']:<12} {px}{risk}  [{status}]")
            if o.get("note"):
                logger.info(f"       {o['note']}")
    if result["unmanaged"]:
        logger.info(f"\nUNMANAGED positions (no plan on record, left alone): {', '.join(result['unmanaged'])}")


def _load_order(sb, order_id: int) -> dict:
    r = sb.table("trade_orders").select("*").eq("id", order_id).execute().data
    if not r:
        raise SystemExit(f"No trade_orders row {order_id}.")
    o = r[0]
    for k in ("qty", "limit_price", "stop_price", "target_price"):
        o[k] = float(o[k]) if o.get(k) is not None else None
    o["report_date"] = _d(o.get("report_date"))
    return o


def _set_status(sb, ids, status: str, **extra):
    sb.table("trade_orders").update({"status": status, "updated_at": datetime.now(timezone.utc).isoformat(),
                                     **extra}).in_("id", list(ids)).execute()


def cmd_approve(args, sb, cfg):
    o = _load_order(sb, args.order_id)
    if o["status"] != PROPOSED:
        raise SystemExit(f"Order {o['id']} is {o['status']}; only PROPOSED orders can be approved.")
    _set_status(sb, [o["id"]], "REJECTED" if args.reject else "APPROVED")
    logger.info(f"Order {o['id']} {o['side']} {o['qty']:g} {o['ticker']} → {'REJECTED' if args.reject else 'APPROVED'}")


def cmd_check(args, sb, cfg):
    """Final gate before placement. Exit 0 = place it; anything else = do not."""
    today = today_ny()
    o = _load_order(sb, args.order_id)
    reasons = []
    if o["status"] != "APPROVED":
        reasons.append(f"NOT_APPROVED({o['status']})")
    if _d(o["plan_date"]) != today:
        reasons.append("NOT_TODAYS_ORDER")
    snap = load_snapshot(args.account, None)
    ctx = build_context(sb, snap, today, tradable_universe(cfg), exclude_order_id=o["id"])
    reasons += check_order(o, ctx, quote=args.quote)
    sb.table("trade_orders").update({"guard_reasons": reasons, "checked_at": datetime.now(timezone.utc).isoformat()}) \
        .eq("id", o["id"]).execute()
    if reasons:
        logger.info(f"BLOCKED order {o['id']} {o['side']} {o['qty']:g} {o['ticker']}: {', '.join(reasons)}")
        sys.exit(2)
    logger.info(f"OK to place order {o['id']}: {o['side']} {o['qty']:g} {o['ticker']} {o['order_type']}"
                + (f" limit {o['limit_price']}" if o.get("limit_price") and o["order_type"] == "LIMIT" else "")
                + (f" stop {o['stop_price']}" if o["order_type"] == "STOP" else "")
                + (f" — cancel {o['cancel_first']} first" if o.get("cancel_first") else ""))


def cmd_placed(args, sb, cfg):
    o = _load_order(sb, args.order_id)
    if o["status"] != "APPROVED":
        raise SystemExit(f"Order {o['id']} is {o['status']}, not APPROVED.")
    _set_status(sb, [o["id"]], "PLACED", broker_order_id=args.broker_order_id)
    logger.info(f"Order {o['id']} PLACED (broker id {args.broker_order_id})")


def cmd_fill(args, sb, cfg):
    o = _load_order(sb, args.order_id)
    filled_at = datetime.fromisoformat(args.time) if args.time else datetime.now(NY)
    if filled_at.tzinfo is None:
        raise SystemExit("--time needs a timezone offset.")
    sb.table("trade_fills").insert({
        "order_id": o["id"], "ticker": o["ticker"], "side": o["side"], "qty": args.qty, "price": args.price,
        "filled_at": filled_at.isoformat(), "trade_date": filled_at.astimezone(NY).date().isoformat(),
        "broker_order_id": o.get("broker_order_id"), "source": o.get("account_source") or "robinhood",
    }).execute()
    prior = sum(float(f["qty"]) for f in sb.table("trade_fills").select("qty").eq("order_id", o["id"]).execute().data)
    status = "FILLED" if prior >= o["qty"] - 1e-9 else "PARTIAL"
    _set_status(sb, [o["id"]], status)
    logger.info(f"Fill recorded: {o['side']} {args.qty:g} {o['ticker']} @ {args.price} → order {o['id']} {status}")


def cmd_cancel(args, sb, cfg):
    _set_status(sb, [args.order_id], "EXPIRED" if args.expired else "CANCELLED")
    logger.info(f"Order {args.order_id} → {'EXPIRED' if args.expired else 'CANCELLED'}")


def cmd_halt(args, sb, cfg, halted: bool):
    sb.table("trading_controls").upsert({"id": 1, "halted": halted, "halt_reason": args.reason if halted else None,
                                         "updated_at": datetime.now(timezone.utc).isoformat()}).execute()
    logger.info("New entries HALTED." if halted else "Trading resumed.")


def cmd_symbols(args, sb, cfg):
    """Tickers the morning task must quote on Robinhood: today's BUY plans,
    carried-over entries, and held positions. Prints a JSON list."""
    today = today_ny()
    run_id, reports = _latest_swing_run(sb)
    todays = {r["ticker"]: r["signal"] for r in reports}
    syms = {r["ticker"] for r in reports if r["signal"] == "BUY" and r.get("entry_price")}
    syms |= {p["ticker"] for p in _carryover(sb, today, todays)}
    if args.positions:
        with open(args.positions, encoding="utf-8") as f:
            raw = json.load(f)
        syms |= {(x.get("symbol") or "").upper() for x in (raw.get("data", raw) or {}).get("positions", [])
                 if float(x.get("quantity") or 0) > 0}
    syms.discard("")
    print(json.dumps(sorted(syms)))


def cmd_show(args, sb, cfg):
    rows = (sb.table("trade_orders").select("*").eq("plan_date", today_ny().isoformat())
            .order("id").execute().data or [])
    if not rows:
        logger.info("No orders today.")
    for o in rows:
        reasons = f" {','.join(o['guard_reasons'])}" if o.get("guard_reasons") else ""
        logger.info(f"  #{o['id']:<5} {o['status']:<9} {o['side']:<4} {float(o['qty']):>5g} {o['ticker']:<5} "
                    f"{o['intent']:<12} {o['order_type']:<6} lim {o.get('limit_price')} stop {o.get('stop_price')}{reasons}")


def main():
    p = argparse.ArgumentParser(description="Orblex Industries — swing trade plan and pre-trade checks")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("plan")
    src = sp.add_mutually_exclusive_group(required=True)
    src.add_argument("--account", help="Broker account snapshot JSON")
    src.add_argument("--paper", type=float, help="Empty paper account with this equity")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--replace", action="store_true", help="Rebuild today's plan, expiring its unapproved orders")
    sp.add_argument("--quotes", nargs="+", help="Saved Robinhood get_equity_quotes response(s) (required live)")
    sy = sub.add_parser("symbols"); sy.add_argument("--positions", help="Saved get_equity_positions response")
    sa = sub.add_parser("approve"); sa.add_argument("order_id", type=int); sa.add_argument("--reject", action="store_true")
    sc = sub.add_parser("check"); sc.add_argument("order_id", type=int)
    sc.add_argument("--account", required=True); sc.add_argument("--quote", type=float)
    sl = sub.add_parser("placed"); sl.add_argument("order_id", type=int); sl.add_argument("--broker-order-id", required=True)
    sf = sub.add_parser("fill"); sf.add_argument("order_id", type=int)
    sf.add_argument("--qty", type=float, required=True); sf.add_argument("--price", type=float, required=True)
    sf.add_argument("--time", help="ISO time with offset (default: now)")
    sx = sub.add_parser("cancel"); sx.add_argument("order_id", type=int); sx.add_argument("--expired", action="store_true")
    sh = sub.add_parser("halt"); sh.add_argument("--reason", required=True)
    sub.add_parser("resume")
    sub.add_parser("show")
    args = p.parse_args()

    cfg = load_config()
    sb = get_client(cfg.supabase_url, cfg.supabase_key)
    {"plan": cmd_plan, "approve": cmd_approve, "check": cmd_check, "placed": cmd_placed,
     "fill": cmd_fill, "cancel": cmd_cancel, "show": cmd_show, "symbols": cmd_symbols,
     "halt": lambda a, s, c: cmd_halt(a, s, c, True),
     "resume": lambda a, s, c: cmd_halt(a, s, c, False)}[args.cmd](args, sb, cfg)


if __name__ == "__main__":
    main()
