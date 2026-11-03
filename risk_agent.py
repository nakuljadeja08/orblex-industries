"""
risk_agent.py — Claude-powered Risk Manager Agent.

Migrated from OpenAI → Anthropic for consistency with the rest of the pipeline.
"""

import json
import logging
import re
from typing import Optional

import anthropic

from llm import ask, decision_model

logger = logging.getLogger(__name__)

# Model and per-model request rules: llm.py.

# ticker_details carries one object per ticker, so the output budget scales with
# the universe (~31 names today). 4096 was not enough headroom.
MAX_TOKENS = 8192

# Applied to any ticker the model fails to return. risk_score >= 8 is the
# allocator's exclusion threshold, so an unassessed ticker is held at 0% rather
# than silently sized as an ordinary position.
MISSING_RISK_SCORE   = 8
MISSING_MAX_POSITION = 0.0

SYSTEM_PROMPT = """You are the Chief Risk Officer at Orblex Industries, an autonomous AI-powered portfolio management system.

Your role is to evaluate portfolio risk AFTER the Analyst Agent has produced Buy/Sell/Hold signals.
You do NOT generate alpha — you protect capital by setting limits and flagging danger.

You will receive:
- A distribution of analyst signals across the portfolio (BUY / SELL / HOLD counts)
- Per-ticker data: signal, signal strength, annualised volatility, anomaly count,
  anomaly types, days to next earnings, and options positioning (put/call, IV
  rank, flow signal)
- Portfolio-level stats: average volatility, total anomalies
- Market context: the deterministic regime (with any risk floor) and a macro snapshot
- Backtest feedback: realised portfolio alpha vs SPY, current paper-book drawdown,
  and the tickers whose analyst signals have most often been WRONG historically

Your output MUST be a single valid JSON object with exactly these fields:

{
  "market_risk_level": "LOW" | "MODERATE" | "HIGH" | "CRITICAL",
  "concentration_risk": "LOW" | "MODERATE" | "HIGH",
  "volatility_risk": "LOW" | "MODERATE" | "HIGH",
  "risk_summary": "<2-3 sentence plain-English summary>",
  "reasoning": "<4-6 sentence explanation referencing specific data points>",
  "flagged_tickers": ["TICKER1", "TICKER2"],
  "ticker_details": [
    {
      "ticker": "AAPL",
      "risk_score": 3,
      "max_position_pct": 20.0,
      "risk_flags": ["HIGH_VOLATILITY"],
      "notes": "<1 sentence rationale>"
    }
  ]
}

Rules:
- Output ONLY the JSON. No preamble, no markdown fences, no commentary.
- Include ALL tickers from the input in ticker_details — never drop any.
- risk_score: 1 (very safe) to 10 (very dangerous). Use the full scale.
- max_position_pct: max % of portfolio this ticker should represent (5 to 40).
  - Strong BUY + low volatility + no anomalies → up to 30-40%
  - SELL signals or high volatility → cap at 5-10%
  - Default HOLD with moderate vol → 15-20%
- risk_flags: HIGH_VOLATILITY, OVERBOUGHT, OVERSOLD, SIGNAL_CONFLICT,
  ANOMALY_CLUSTER, LOW_LIQUIDITY_SIGNAL, BEARISH_MOMENTUM, BULL_TRAP_RISK
- flagged_tickers: tickers with risk_score >= 7 OR max_position_pct <= 8
- market_risk_level:
  - CRITICAL: >60% SELL signals OR avg volatility > 0.60
  - HIGH: >40% SELL signals OR avg volatility > 0.40
  - MODERATE: mixed signals or avg volatility 0.20-0.40
  - LOW: majority BUY/HOLD with low volatility
- For excluded anomaly tickers (risk_score>=8), add a note in watchdog_notes
  explaining why they were excluded — do not leave it empty.
- EARNINGS PROXIMITY is event risk: a ticker reporting within ~5 trading days
  should carry a higher risk_score and a tighter max_position_pct than its
  volatility alone implies — an earnings gap is not diversifiable. Say so in its
  notes. Distant earnings (>30 days) need no such adjustment.
- OPTIONS POSITIONING is forward-looking risk: a high IV rank means the market is
  pricing a large move (raise risk_score); an elevated put/call ratio or a BEARISH
  flow signal on a name the analyst wants to BUY is a conflict worth flagging.
- MARKET CONTEXT sets the floor: a CORRECTION/BEAR regime or a hostile macro
  equity impact should lift market_risk_level and compress position caps
  portfolio-wide, independent of any single name.
- Use BACKTEST FEEDBACK to protect capital: a ticker listed among the lowest
  historical hit rates warrants a tighter max_position_pct (and a note) even when
  its current volatility looks benign, because the analyst has been repeatedly
  wrong on it. A deeply negative paper-book drawdown or persistent negative alpha
  vs SPY argues for raising market_risk_level. Feedback shown as "N/A" has no
  matured history yet — do not treat it as a good or bad result.
- This is for simulation and research purposes only.
"""


def _build_prompt(ctx: dict) -> str:
    sc    = ctx["signal_counts"]
    total = ctx["total_tickers"]
    sell_pct = round((sc.get("SELL", 0) / total) * 100) if total > 0 else 0

    def _na(v):
        return v if v is not None else "N/A"

    ticker_lines = []
    for t in ctx["ticker_inputs"]:
        vol_str   = f"{t['volatility']:.2%}" if t["volatility"] else "N/A"
        anom_types = [a.get("type", "unknown") for a in t.get("anomalies", [])]
        anom_str  = ", ".join(anom_types) if anom_types else "none"
        dte = t.get("days_to_earnings")
        earn_str = f"{dte}d ({_na(t.get('earnings_risk'))})" if dte is not None else "N/A"
        ticker_lines.append(
            f"  {t['ticker']}: signal={t['signal']} ({t['signal_strength']}), "
            f"vol={vol_str}, anomalies={t['anomaly_count']} [{anom_str}], "
            f"earnings={earn_str}, "
            f"pc={_na(t.get('put_call_ratio'))}, iv_rank={_na(t.get('iv_rank'))}, "
            f"flow={_na(t.get('flow_signal'))}"
        )

    mkt    = ctx.get("market_context") or {}
    regime = mkt.get("regime") or {}
    macro  = mkt.get("macro") or {}
    market_line = (
        f"Regime={_na(regime.get('regime'))} "
        f"(floor: {regime.get('risk_floor') or 'none'}, VIX {_na(regime.get('vix_close'))})  |  "
        f"Macro={_na(macro.get('macro_regime'))}, equity impact {_na(macro.get('equity_impact'))}"
    )

    return f"""Assess portfolio risk as of {ctx['assessment_date']}.

PORTFOLIO SUMMARY
-----------------
Total tickers  : {total}
BUY signals    : {sc.get('BUY', 0)} ({round(sc.get('BUY',0)/total*100) if total else 0}%)
SELL signals   : {sc.get('SELL', 0)} ({sell_pct}%)
HOLD signals   : {sc.get('HOLD', 0)} ({round(sc.get('HOLD',0)/total*100) if total else 0}%)
Avg volatility : {f"{ctx['avg_volatility']:.2%}" if ctx['avg_volatility'] else 'N/A'}
Total anomalies: {ctx['total_anomalies']}

MARKET CONTEXT
--------------
{market_line}

PER-TICKER INPUTS (earnings=days-to-report, pc=put/call vol, flow=options signal)
--------------------------------------------------------------------------------
{chr(10).join(ticker_lines)}

BACKTEST FEEDBACK (realised outcomes of prior runs)
---------------------------------------------------
{ctx.get('performance_feedback') or '  No backtest feedback available yet (system still accumulating history).'}

IMPORTANT: ticker_details in your response MUST include ALL {total} tickers listed above.
Do not drop any ticker — every ticker that enters the analyst stage must have a risk record.

Produce the risk assessment JSON now."""


def assess_risk(ctx: dict, api_key: Optional[str] = None) -> Optional[dict]:
    """
    Calls Claude with the risk context and returns a parsed risk assessment dict.
    Returns None on failure.
    """
    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_prompt(ctx)

    logger.info(f"  Calling Claude Risk Manager Agent ({decision_model()})...")
    logger.info(f"  Tickers in context: {len(ctx['ticker_inputs'])}")

    try:
        msg = ask("decision", SYSTEM_PROMPT, prompt, MAX_TOKENS, client)
    except Exception as e:
        logger.error(f"  Anthropic API error: {e}")
        return None

    # ticker_details is one object per ticker, so output length scales with the
    # universe. Truncation here silently drops tickers, which is why the
    # placeholder path below must fail closed rather than default to moderate.
    truncated = msg.stop_reason == "max_tokens"
    if truncated:
        logger.error(
            f"  Risk response hit max_tokens ({MAX_TOKENS}) — JSON is truncated and "
            f"tickers will be missing. Raise MAX_TOKENS or shrink per-ticker output."
        )

    raw = msg.text.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'\s*```\s*$',       '', raw, flags=re.MULTILINE)
    raw = raw.strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            try:
                result = json.loads(match.group())
            except json.JSONDecodeError:
                logger.error(f"  Failed to parse risk JSON. Raw:\n{raw[:300]}")
                return None
        else:
            logger.error(f"  No JSON in risk response. Raw:\n{raw[:300]}")
            return None

    # Validate coverage — every ticker that entered the analyst stage must leave
    # this stage with a risk record.
    expected = {t["ticker"] for t in ctx["ticker_inputs"]}
    received = {td["ticker"] for td in result.get("ticker_details", [])}
    missing  = sorted(expected - received)

    if missing:
        # Fail CLOSED. A ticker with no risk assessment is an unknown, and an
        # unknown must not be sized. The previous placeholder (risk_score=5,
        # max_position_pct=15.0) made a dropped ticker indistinguishable from a
        # deliberately moderate one, so a truncated response quietly produced
        # ordinary positions in names nobody had assessed.
        logger.error(
            f"  Claude returned no risk record for {len(missing)} ticker(s): "
            f"{', '.join(missing)}"
        )
        logger.error(
            f"  Failing closed — each is pinned to risk_score={MISSING_RISK_SCORE} "
            f"/ max_position_pct={MISSING_MAX_POSITION}% and will be EXCLUDED by "
            f"the allocator."
        )
        for ticker in missing:
            result["ticker_details"].append({
                "ticker":           ticker,
                "risk_score":       MISSING_RISK_SCORE,
                "max_position_pct": MISSING_MAX_POSITION,
                "risk_flags":       ["ASSESSMENT_MISSING"],
                "notes":            "No risk assessment returned by the model"
                                    + (" (response truncated)" if truncated else "")
                                    + " — excluded as a precaution, not a risk judgement.",
            })

        # Surface it on the portfolio-level record too, so a degraded run is
        # visible in the newsletter and to the Watchdog rather than only in logs.
        flagged = list(result.get("flagged_tickers") or [])
        result["flagged_tickers"] = flagged + [t for t in missing if t not in flagged]

    result["response_truncated"] = truncated
    result["missing_tickers"]    = missing
    result["coverage_complete"]  = not missing and not truncated

    result["prompt_tokens"] = msg.input_tokens
    result["output_tokens"] = msg.output_tokens
    result["model"]         = msg.model

    return result