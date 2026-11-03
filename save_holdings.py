"""
save_holdings.py — the weekly "Now" column of the LONG TERM tab: the MAIN account's
weights from a read-only snapshot (EXECUTOR_DESIGN.md §2.1).

The scheduled task reads the main account through the Robinhood MCP's read-only
tools (get_portfolio, get_equity_positions, get_equity_quotes), saves the raw
responses locally, and runs this. Only each holding's percentage of total value
is stored (longterm_holdings): no share counts, prices, dollar values or account
size leave this machine. The table is readable only by signed-in trading admins.

Usage:
  python save_holdings.py --portfolio p.json --positions pos.json --quotes q1.json [q2.json] [--dry-run]
"""

import argparse
import json
import sys
from datetime import date

from price_checks import parse_quotes

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


def _data(x):
    return x.get("data", x) if isinstance(x, dict) else {}


def weights(portfolio: dict, positions: dict, quotes: dict, at_cost: list | None = None,
            inactive: list | None = None) -> dict[str, float]:
    """{ticker: % of total account value}. Uses the live price, then the official close,
    then (for a holding Robinhood returns no quote for, e.g. halted or delisted) the
    position's average cost, which is listed in `at_cost` so the caller can say so.
    Inactive instruments (Robinhood marks them with a trailing "^", e.g. a delisted
    residual) cannot be quoted or traded: they are left out and listed in `inactive`.
    Raises only if a held ticker has no price at all (a silent gap would show the
    holding as 0% and the advice would say NEW/ADD)."""
    total = float(_data(portfolio).get("total_value") or 0)
    if total <= 0:
        raise ValueError("portfolio total_value missing or zero")
    out, missing = {}, []
    for p in _data(positions).get("positions", []) or []:
        qty = float(p.get("quantity") or 0)
        if qty <= 0:
            continue
        sym = p["symbol"].upper()
        if sym.endswith("^"):
            if inactive is not None:
                inactive.append(sym)
            continue
        q = quotes.get(sym) or {}
        px = q.get("live") or q.get("official_close")
        if not px:
            cost = float(p.get("average_buy_price") or 0)
            if cost > 0 and at_cost is not None:
                px = cost
                at_cost.append(sym)
            else:
                missing.append(sym)
                continue
        out[sym] = round(qty * px / total * 100, 2)
    if missing:
        raise ValueError(f"no price for held ticker(s): {', '.join(missing)}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Save the main account's weights for the LONG TERM tab")
    ap.add_argument("--portfolio", required=True)
    ap.add_argument("--positions", required=True)
    ap.add_argument("--quotes", nargs="+", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load = lambda f: json.load(open(f, encoding="utf-8"))
    at_cost, inactive = [], []
    w = weights(load(args.portfolio), load(args.positions), parse_quotes(*[load(q) for q in args.quotes]),
                at_cost, inactive)
    today = date.today().isoformat()
    print(f"{len(w)} holding(s), {sum(w.values()):.1f}% invested"
          + (f" · NO QUOTE, valued at average cost: {', '.join(at_cost)}" if at_cost else "")
          + (f" · inactive, left out: {', '.join(inactive)}" if inactive else ""))
    for t, pct in sorted(w.items(), key=lambda kv: -kv[1]):
        print(f"  {t:<6} {pct:>6.2f}%")
    if not args.dry_run:
        from config import load_config
        from loader import get_client
        cfg = load_config()
        sb = get_client(cfg.supabase_url, cfg.supabase_key)
        sb.table("longterm_holdings").delete().eq("snapshot_date", today).execute()
        if w:
            sb.table("longterm_holdings").insert(
                [{"snapshot_date": today, "ticker": t, "weight_pct": pct} for t, pct in w.items()]).execute()
        print(f"Saved snapshot {today}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
