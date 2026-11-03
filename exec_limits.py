"""
exec_limits.py — the hard limits on how much the system may trade (EXECUTOR_DESIGN.md §1.3).

Three layers, each can only TIGHTEN the one above it:

  1. CEILINGS (this file)   constants with tests; changing one needs a commit.
                            trade_guard imports its caps from here, so the manual
                            plan and the future executor share one set of numbers.
  2. Daily mandate          what the user approves each morning on the dashboard
                            (trading_mandates, set_trading_mandate RPC): tickers,
                            entries, total risk. effective_limits() takes the
                            tighter of mandate and ceiling for every field.
  3. Per-order approval     Robinhood's in-app approval queue (or the dashboard).

check_submission() is the single gate an executor calls before EVERY order it
submits: trade_guard.check_order (account, day-trade, caps) + the execution-rate
ceilings below + the mandate. An empty list means submit; anything else means
don't. It FAILS CLOSED: missing execution state, a missing/expired/revoked
mandate (for entries) or an unknown intent is a reason, never a pass.

Exits are not gated by the mandate or the halts (they reduce risk), but they do
count toward the daily order cap and the per-order retry limits, and the
day-trade rules still apply.

How the risk numbers fit together (reviewed 2026-11-02, EXECUTOR_DESIGN.md §1.3):
  MAX_OPEN_RISK_PCT is the limit that binds on risk. Two full-size STRONG trades
  (2 × 2.5%) use all of it, so five open positions only fit while each risks about 1%
  or less. On a small book that is the normal case: WEAK plans (× 0.5) and whole-share
  rounding often size entries well under 1% of equity. So
  MAX_OPEN_POSITIONS is only a count of open names, and RISK_PER_TRADE_PCT is the most
  one trade may take, not a typical size. One full stop-out (2.5%) stays under
  DAILY_LOSS_HALT_PCT; a second one trips it.
"""

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Optional

# ── Layer 1: ceilings (accepted 2026-10-15 for a $500–5k account) ─────────────
RISK_PER_TRADE_PCT    = 2.5     # equity at risk entry → stop, before strength scaling
                                # (1.0 → 2.5 on 2026-10-24: a small book sizes to 0 whole shares at 1%)
MAX_NEW_ENTRIES_DAY   = 2
MAX_ENTRIES_5_SESSIONS = 6      # rolling: today + the 4 sessions before it
MAX_OPEN_POSITIONS    = 5       # positions + working entry orders (3 → 5 on 2026-10-22, user decision)
MAX_ORDERS_DAY        = 8       # every order submitted today, all intents
MAX_RESUBMITS         = 1       # one retry per order, then a human
MAX_CANCEL_REPLACE    = 2       # per order per day
MAX_POSITION_PCT      = 30.0    # notional per position, % of equity
MAX_OPEN_RISK_PCT     = 5.0     # open + new risk to stops, % of equity
MAX_STOP_OUTS_DAY     = 2       # consecutive stop-outs today → no more entries today
DAILY_LOSS_HALT_PCT   = 3.0
DRAWDOWN_HALT_PCT     = 10.0
PDT_MAX_DAY_TRADES    = 3       # per rolling 5 sessions while equity < PDT_EQUITY
PDT_EQUITY            = 25_000.0
# The user confirmed 2026-11-02: same-day exits are allowed, same-day re-buys are not
# (EXECUTOR_DESIGN.md §1.6). trade_guard still refuses the 4th day trade in 5 sessions.
# False restores the old rule (nothing sold on the fill day, no stop that day).
ALLOW_SAME_DAY_EXIT   = True
ENTRY_ORDER_TYPES     = frozenset({"LIMIT"})

ENTRY = "ENTRY"
EXIT_INTENTS = ("EXIT_SIGNAL", "EXIT_TIME", "EXIT_STOP", "EXIT_TARGET", "PROTECT_STOP")
STOP_OUT_INTENTS = ("EXIT_STOP", "PROTECT_STOP")
KINDS = ("SUBMIT", "RESUBMIT", "CANCEL", "REPLACE")


# ── Layer 2: the daily mandate ─────────────────────────────────────────────────

@dataclass
class Mandate:
    mandate_date: date
    max_entries: int
    max_total_risk_usd: float
    allowed_tickers: frozenset
    order_types: frozenset = ENTRY_ORDER_TYPES
    require_stop: bool = True
    expires_at: Optional[datetime] = None
    status: str = "ACTIVE"

    @classmethod
    def from_row(cls, r: dict) -> "Mandate":
        exp = r.get("expires_at")
        return cls(
            mandate_date=date.fromisoformat(str(r["mandate_date"])[:10]),
            max_entries=int(r["max_entries"]),
            max_total_risk_usd=float(r["max_total_risk_usd"]),
            allowed_tickers=frozenset(t.upper() for t in (r.get("allowed_tickers") or [])),
            order_types=frozenset(r.get("order_types") or ENTRY_ORDER_TYPES),
            require_stop=bool(r.get("require_stop", True)),
            expires_at=datetime.fromisoformat(str(exp).replace("Z", "+00:00")) if exp else None,
            status=r.get("status") or "ACTIVE",
        )


@dataclass
class EffectiveLimits:
    max_entries: int
    max_total_risk_usd: float
    allowed_tickers: frozenset
    order_types: frozenset
    require_stop: bool


def effective_limits(m: Mandate) -> EffectiveLimits:
    """The tighter of mandate and ceiling, field by field: a mandate can never loosen a ceiling."""
    return EffectiveLimits(
        max_entries=max(0, min(m.max_entries, MAX_NEW_ENTRIES_DAY)),
        max_total_risk_usd=max(0.0, m.max_total_risk_usd),
        allowed_tickers=m.allowed_tickers,
        order_types=frozenset(m.order_types) & ENTRY_ORDER_TYPES,
        require_stop=True,                                              # a stop is always required
    )


def mandate_problem(m: Optional[Mandate], today: date, now: datetime) -> Optional[str]:
    if m is None:
        return "NO_MANDATE"
    if m.status != "ACTIVE":
        return "MANDATE_REVOKED"
    if m.mandate_date != today or (m.expires_at is not None and now >= m.expires_at):
        return "MANDATE_EXPIRED"
    return None


# ── Execution state (what has already happened) ───────────────────────────────

@dataclass
class ExecState:
    """Everything the rate ceilings need. complete=False when any source could not be
    read; check_submission then refuses everything (fail closed)."""
    today: date
    orders_submitted_today: int = 0
    submits_by_order: dict = field(default_factory=dict)          # order_id → SUBMIT + RESUBMIT count (all time)
    cancel_replace_today: dict = field(default_factory=dict)      # order_id → CANCEL + REPLACE today
    entries_today: set = field(default_factory=set)               # tickers with an entry submitted or filled today
    entries_5_sessions: int = 0                                   # entries over today + 4 prior sessions
    entry_risk_today: float = 0.0                                 # $ risk of entries submitted/filled today
    exits_today: list = field(default_factory=list)               # exit intents of today's SELL fills, in time order
    day_trades_5_sessions: int = 0
    complete: bool = True


def consecutive_stop_outs(exits: list) -> int:
    n = 0
    for intent in reversed(exits):
        if intent in STOP_OUT_INTENTS:
            n += 1
        else:
            break
    return n


# ── The gate ──────────────────────────────────────────────────────────────────

def check_submission(order: dict, guard_ctx, state: Optional[ExecState], mandate: Optional[Mandate],
                     now: datetime, quote: Optional[float] = None, kind: str = "SUBMIT") -> list[str]:
    """Reasons NOT to submit this order now (empty list = submit). kind: SUBMIT | RESUBMIT | REPLACE."""
    from trade_guard import check_order    # trade_guard imports this module's constants

    intent = order.get("intent")
    is_entry = intent == ENTRY
    if guard_ctx.day_trades_5_sessions is None and state is not None and state.complete:
        guard_ctx = replace(guard_ctx, day_trades_5_sessions=state.day_trades_5_sessions)
    reasons = set(check_order(order, guard_ctx, quote))

    if kind not in ("SUBMIT", "RESUBMIT", "REPLACE"):
        reasons.add("UNKNOWN_KIND")
    if state is None or not state.complete or state.today != guard_ctx.today:
        reasons.add("EXEC_STATE_UNKNOWN")
        return sorted(reasons)

    # Rate ceilings: every intent.
    if state.orders_submitted_today + 1 > MAX_ORDERS_DAY:
        reasons.add("MAX_ORDERS_TODAY")
    oid = order.get("id")
    if kind == "RESUBMIT" and state.submits_by_order.get(oid, 0) >= 1 + MAX_RESUBMITS:
        reasons.add("RESUBMIT_LIMIT")
    if kind == "SUBMIT" and state.submits_by_order.get(oid, 0) >= 1:
        reasons.add("ALREADY_SUBMITTED")            # a retry must say RESUBMIT and count against the limit
    if kind == "REPLACE" and state.cancel_replace_today.get(oid, 0) >= MAX_CANCEL_REPLACE:
        reasons.add("CANCEL_REPLACE_LIMIT")

    if is_entry:
        if len(state.entries_today) + 1 > MAX_NEW_ENTRIES_DAY:
            reasons.add("MAX_ENTRIES_TODAY")
        if state.entries_5_sessions + 1 > MAX_ENTRIES_5_SESSIONS:
            reasons.add("MAX_ENTRIES_WEEK")
        if consecutive_stop_outs(state.exits_today) >= MAX_STOP_OUTS_DAY:
            reasons.add("STOP_OUT_HALT")

        problem = mandate_problem(mandate, guard_ctx.today, now)
        if problem:
            reasons.add(problem)
        else:
            lim = effective_limits(mandate)
            t = str(order.get("ticker", "")).upper()
            if t not in lim.allowed_tickers:
                reasons.add("NOT_IN_MANDATE")
            if order.get("order_type") not in lim.order_types:
                reasons.add("ORDER_TYPE_NOT_ALLOWED")
            if lim.require_stop and not order.get("stop_price"):
                reasons.add("STOP_REQUIRED")
            if len(state.entries_today) + 1 > lim.max_entries:
                reasons.add("MANDATE_MAX_ENTRIES")
            risk = order.get("risk_dollars")
            if risk is None:
                try:
                    risk = float(order["qty"]) * (float(order["limit_price"]) - float(order["stop_price"]))
                except (KeyError, TypeError, ValueError):
                    risk = None
            if risk is None or state.entry_risk_today + float(risk) > lim.max_total_risk_usd + 1e-6:
                reasons.add("MANDATE_RISK_BUDGET")
    elif intent not in EXIT_INTENTS:
        reasons.add("UNKNOWN_INTENT")

    return sorted(reasons)
