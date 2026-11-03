"""Tests for broker_snapshot: Robinhood MCP responses → trade-plan snapshot."""

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from broker_snapshot import build_snapshot  # noqa: E402

NOW = datetime(2026, 10, 12, 9, 20, tzinfo=ZoneInfo("America/New_York"))
PORTFOLIO = {"data": {"total_value": "5000", "cash": "5000", "pending_deposits": "0",
                      "buying_power": {"buying_power": "10000.0000", "unleveraged_buying_power": "5000.0000"}}}


def test_uses_unleveraged_buying_power():
    s = build_snapshot(PORTFOLIO, {"positions": []}, {"orders": []}, now=NOW)
    assert s["equity"] == 5000 and s["buying_power"] == 5000   # not the 2x margin figure


def test_intraday_quantity_counts_as_buy_today():
    pos = {"positions": [{"symbol": "NVDA", "quantity": "4", "average_buy_price": "227.5",
                          "intraday_quantity": "4", "shares_available_for_sells": "4"}]}
    s = build_snapshot(PORTFOLIO, pos, {"orders": []}, now=NOW)
    assert {"NVDA"} == {f["ticker"] for f in s["fills_today"] if f["side"] == "BUY"}


def test_orders_map_to_fills_and_open_orders():
    orders = {"orders": [
        {"id": "a", "symbol": "AAPL", "side": "sell", "state": "filled", "type": "market",
         "quantity": "1", "cumulative_quantity": "1", "average_price": "330",
         "last_transaction_at": "2026-10-12T13:31:00Z"},                       # 09:31 ET today
        {"id": "b", "symbol": "MU", "side": "buy", "state": "filled", "type": "limit",
         "quantity": "1", "cumulative_quantity": "1", "last_transaction_at": "2026-10-09T14:00:00Z"},
        {"id": "c", "symbol": "NVDA", "side": "sell", "state": "confirmed", "type": "market",
         "trigger": "stop", "quantity": "4", "cumulative_quantity": "0", "stop_price": "223"},
        {"id": "d", "symbol": "NOW", "side": "buy", "state": "partially_filled", "type": "limit",
         "quantity": "8", "cumulative_quantity": "3", "price": "138.5",
         "last_transaction_at": "2026-10-12T14:00:00Z"},
    ]}
    s = build_snapshot(PORTFOLIO, {"positions": []}, orders, now=NOW)
    fills = {(f["ticker"], f["side"]) for f in s["fills_today"]}
    assert fills == {("AAPL", "SELL"), ("NOW", "BUY")}                # MU filled on a prior day
    opened = {(o["ticker"], o["type"], o["qty"]) for o in s["open_orders"]}
    assert opened == {("NVDA", "STOP", 4.0), ("NOW", "LIMIT", 5.0)}


def test_late_utc_evening_is_still_today_in_new_york():
    orders = {"orders": [{"id": "x", "symbol": "MU", "side": "buy", "state": "filled", "type": "limit",
                          "quantity": "1", "cumulative_quantity": "1",
                          "last_transaction_at": "2026-10-12T19:59:00Z"}]}
    later = datetime(2026, 10, 12, 16, 30, tzinfo=ZoneInfo("America/New_York"))
    assert build_snapshot(PORTFOLIO, {"positions": []}, orders, now=later)["fills_today"][0]["ticker"] == "MU"


def test_quotes_set_last_price():
    pos = {"positions": [{"symbol": "NVDA", "quantity": "4", "average_buy_price": "227.5"}]}
    # Real get_equity_quotes shape: data.results[].quote + .close
    q = {"data": {"results": [{"quote": {"symbol": "NVDA", "last_trade_price": "231.10",
                                         "venue_last_trade_time": "2026-10-12T13:20:00Z", "state": "active"},
                               "close": {"symbol": "NVDA", "date": "2026-10-09", "price": "229.80"}}]}}
    s = build_snapshot(PORTFOLIO, pos, {"orders": []}, q, now=NOW)
    assert s["positions"][0]["last_price"] == 231.10
