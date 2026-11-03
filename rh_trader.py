"""
rh_trader.py — the ONLY module that may send orders to Robinhood (EXECUTOR_DESIGN.md phase 5).

rh_client stays read-only. This wraps the same signed-in session with a wider, still closed,
list of tools, and refuses every order call unless ALL of these hold:

  1. the switch is on: EXECUTOR_LIVE=1 (a GitHub repository variable only you can set;
     unset or anything else = off)
  2. the account is the agentic account, RH_AGENTIC_ACCOUNT (never the main account)
  3. Robinhood's own setting says human_must_approve_trades = true, read in this same
     session (approve_gate()). If you turn the approval queue off in the app, the
     executor stops sending: every order must wait for your tap.

Orders sent this way do NOT execute by themselves: they land in the Robinhood app's approval
queue, and pending approvals expire on their own if you ignore them.
"""

import os

from rh_client import AGENTIC_ACCOUNT, READ_TOOLS, NotAllowed, ReadOnlyRobinhood, mask

ORDER_TOOLS = frozenset({"place_equity_order", "place_advanced_order",
                         "cancel_equity_order", "cancel_advanced_order", "decline_trade_approval"})
EXTRA_READ_TOOLS = frozenset({"get_advanced_orders"})
SWITCH = "EXECUTOR_LIVE"


def live_switch_on() -> bool:
    return os.environ.get(SWITCH, "").strip() == "1"


def guard_trade_call(name: str, args: dict | None, approval_confirmed: bool) -> None:
    """Raise NotAllowed unless this call is allowed right now."""
    acct = (args or {}).get("account_number")
    if name in READ_TOOLS | EXTRA_READ_TOOLS:
        if acct is not None and str(acct) != AGENTIC_ACCOUNT:
            raise NotAllowed(f"account {mask(acct)} is not the agentic account")
        return
    if name not in ORDER_TOOLS:
        raise NotAllowed(f"{name} is not a tool the executor uses")
    if str(acct) != AGENTIC_ACCOUNT:
        raise NotAllowed(f"account {mask(acct)} is not the agentic account")
    if not live_switch_on():
        raise NotAllowed(f"the live switch is off ({SWITCH} != 1)")
    if not approval_confirmed:
        raise NotAllowed("Robinhood's approval queue is not confirmed on (human_must_approve_trades)")


class TradingRobinhood(ReadOnlyRobinhood):
    """Pass as open_robinhood(handle=TradingRobinhood). Call approve_gate() before any order."""

    def __init__(self, session):
        super().__init__(session)
        self.approval_confirmed = False

    async def approve_gate(self) -> bool:
        res = await self.call("get_trade_approval_setting", {"account_number": AGENTIC_ACCOUNT})
        st = (res.get("data", res) if isinstance(res, dict) else {}).get("setting", {}) or {}
        self.approval_confirmed = st.get("human_must_approve_trades") is True
        return self.approval_confirmed

    async def call(self, name: str, args: dict | None = None):
        guard_trade_call(name, args, self.approval_confirmed)
        if name in READ_TOOLS:
            return await super().call(name, args)
        import json
        from rh_client import _text
        res = await self.session.call_tool(name, args or {})
        if getattr(res, "isError", False):
            raise RuntimeError(f"{name} failed: {_text(res)[:300]}")
        if getattr(res, "structuredContent", None):
            return res.structuredContent
        txt = _text(res)
        try:
            return json.loads(txt)
        except ValueError:
            return txt
