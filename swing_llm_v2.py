"""
swing_llm_v2.py — the only model calls in the PIPELINE_V2 swing book (EXECUTOR_DESIGN.md §3).

The script (swing_screen.py) has already chosen the candidates and set every
level. The model gets two narrow jobs:

  news_read()       fast model, once per candidate that HAS headlines (none → no
                    call): is there a catalyst that makes a 1–7 day long a bad
                    idea (binary event, guidance cut, offering, deal break)?
  rank_and_veto()   decision model, ONE call for all candidates: order them and
                    veto any that should not be bought, with a reason.

apply_decisions() then DISPOSES, in Python: the model cannot add a ticker,
change an entry or target, or loosen a stop. The one level it may move is the
stop, and only tighter, staying ≥ MIN_STOP_ATR from the entry. A model failure
leaves the script's picks unvetoed and is recorded, never silently dropped.
"""

import json
import logging
import re
from typing import Optional

from llm import ask
from swing_policy import MIN_STOP_ATR

logger = logging.getLogger(__name__)

CATALYSTS = ("NONE", "POSITIVE", "NEGATIVE", "BINARY_EVENT")
MAX_HEADLINES = 8

NEWS_SYSTEM = (
    "You screen recent headlines for a long-only swing trade held 1 to 7 trading days. "
    "Judge only whether the news creates a specific, near-term reason NOT to buy: a binary event "
    "inside the window (FDA ruling, trial, court ruling, vote), a guidance cut, an equity offering, "
    "a broken deal, fraud or an accounting issue. Ordinary analyst notes, sector chatter and old "
    "news are not reasons. Reply with JSON only."
)

RANK_SYSTEM = (
    "You review swing-trade candidates that a rule-based screen already selected, with entry, stop "
    "and target set by rules. You may ORDER them and VETO any that should not be bought, but you "
    "cannot add tickers or change the entry or target. You may suggest a tighter stop only if the "
    "headlines or the setup justify it. Prefer fewer, cleaner trades; a veto needs a concrete reason "
    "from the data given. Reply with JSON only."
)


def _json(text: str):
    m = re.search(r"\{.*\}|\[.*\]", text, re.S)
    if not m:
        raise ValueError("no JSON in reply")
    return json.loads(m.group(0))


def news_prompt(ticker: str, company: Optional[str], headlines: list[dict]) -> str:
    lines = [f"- {(h.get('published') or '')[:10]} {h.get('source', '')}: {h.get('title', '')}"
             for h in headlines[:MAX_HEADLINES]]
    return (f"Ticker: {ticker}" + (f" ({company})" if company else "") + "\nHeadlines, newest first:\n"
            + "\n".join(lines)
            + '\n\nReply as {"catalyst": "NONE|POSITIVE|NEGATIVE|BINARY_EVENT", '
              '"veto": true|false, "reason": "<one sentence>"}')


def parse_news(text: str) -> dict:
    d = _json(text)
    cat = str(d.get("catalyst", "NONE")).upper()
    return {"catalyst": cat if cat in CATALYSTS else "NONE",
            "veto": bool(d.get("veto")), "reason": str(d.get("reason", ""))[:300]}


def news_read(ticker: str, company: Optional[str], headlines: list[dict], client=None) -> dict:
    """{"catalyst", "veto", "reason", "tokens"}; no headlines → no call."""
    if not headlines:
        return {"catalyst": "NONE", "veto": False, "reason": "no headlines in window", "tokens": 0}
    r = ask("fast", NEWS_SYSTEM, news_prompt(ticker, company, headlines), 300, client)
    out = parse_news(r.text)
    out["tokens"] = r.input_tokens + r.output_tokens
    return out


def rank_prompt(cands: list[dict], regime: Optional[str]) -> str:
    rows = [{
        "ticker": c["ticker"], "setup": c["setup_type"], "entry": c["entry_price"], "stop": c["stop_price"],
        "target": c["target_price"], "horizon_days": c["horizon_days"], "atr": c.get("atr_14"),
        "last_close": c.get("latest_close"), "rs_vs_spy_20d_pct": c.get("rank_score"),
        "rsi_14": c.get("rsi_14"), "price_vs_ma20_pct": c.get("price_vs_ma20_pct"),
        "volume_ratio": c.get("volume_ratio"), "trading_days_to_earnings": c.get("trading_days_to_earnings"),
        "news": {k: c.get("news", {}).get(k) for k in ("catalyst", "reason")},
    } for c in cands]
    return (f"Market regime: {regime or 'unknown'}\nCandidates (script order, best first):\n"
            + json.dumps(rows, indent=1)
            + '\n\nReply as {"decisions": [{"ticker": "...", "decision": "KEEP|VETO", "rank": 1, '
              '"stop": <number or null, only to tighten>, "reason": "<one sentence>"}]} '
              "covering every candidate.")


def rank_and_veto(cands: list[dict], regime: Optional[str], client=None) -> tuple[list[dict], int]:
    if not cands:
        return [], 0
    r = ask("decision", RANK_SYSTEM, rank_prompt(cands, regime), 1500, client)
    d = _json(r.text)
    decisions = d.get("decisions", d) if isinstance(d, dict) else d
    return list(decisions or []), r.input_tokens + r.output_tokens


def apply_decisions(cands: list[dict], decisions: list[dict]) -> list[dict]:
    """Merge the model's decisions into the script's candidates under the rules above.
    Returns the candidates with llm_decision / llm_rank / llm_reason / stop set,
    ordered KEEP by model rank (then script rank), VETO last."""
    by_t = {str(x.get("ticker", "")).upper(): x for x in decisions if isinstance(x, dict)}
    out = []
    for i, c in enumerate(cands):
        c = dict(c)
        x = by_t.get(c["ticker"])
        news_veto = c.get("news", {}).get("veto", False)
        if x is None:
            c.update(llm_decision="KEEP", llm_rank=None, llm_reason="not covered by the model's reply")
        else:
            dec = str(x.get("decision", "KEEP")).upper()
            c.update(llm_decision="VETO" if dec == "VETO" else "KEEP",
                     llm_rank=x.get("rank") if isinstance(x.get("rank"), int) else None,
                     llm_reason=str(x.get("reason", ""))[:300])
            new_stop = x.get("stop")
            try:
                new_stop = float(new_stop) if new_stop is not None else None
            except (TypeError, ValueError):
                new_stop = None
            atr = c.get("atr_14") or 0
            if (new_stop and c["stop_price"] < new_stop <= c["entry_price"] - MIN_STOP_ATR * atr):
                c["llm_stop_from"] = c["stop_price"]
                c["stop_price"] = round(new_stop, 2)
                c["risk_reward"] = round((c["target_price"] - c["entry_price"]) / (c["entry_price"] - c["stop_price"]), 2)
        if news_veto:
            c["llm_decision"] = "VETO"
            c["llm_reason"] = f"news: {c['news'].get('reason', '')}"[:300]
        c["script_rank"] = i
        out.append(c)
    keep = sorted([c for c in out if c["llm_decision"] == "KEEP"],
                  key=lambda c: (c["llm_rank"] if c["llm_rank"] is not None else 999, c["script_rank"]))
    return keep + [c for c in out if c["llm_decision"] == "VETO"]
