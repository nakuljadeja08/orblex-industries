"""
watchdog_agent.py — Claude-powered Watchdog Agent.

Audits the full pipeline cycle for internal consistency, logic violations,
and signal quality. Does NOT make investment decisions — it validates the
other agents' outputs and flags anything suspicious.
"""

import json
import logging
import re
from typing import Optional

import anthropic

from llm import ask, decision_model

logger = logging.getLogger(__name__)

# Model and per-model request rules: llm.py.

SYSTEM_PROMPT = """You are the Watchdog at Orblex Industries, an autonomous AI-powered portfolio management system.

Your job is to audit the entire pipeline cycle — Analyst → Risk Manager → Allocator → Report Writer — and flag any issues. You are the last line of defence before this system's output is stored as an auditable record.

You are NOT a portfolio manager. You do NOT override decisions. You observe, audit, and report.

CRITICAL ACCOUNTING RULE — READ CAREFULLY:
The portfolio allocator uses this invariant:
  sum(ALL ticker allocations including 0% excluded rows) + cash_pct = 100%

invested_pct is the sum of ONLY active (non-zero) tickers. It will NEVER equal
100% - cash_pct when any tickers are excluded. This is BY DESIGN, not a bug.

Example of correct output (NOT a mismatch):
  AAPL: 25.1%,  TSLA: 0% (EXCLUDED),  MDB: 0% (EXCLUDED),  cash: 60.4%
  ticker_sum = 25.1% + 0% + 0% = 25.1%
  total = 25.1% + 60.4% = 85.5%  ← NOT 100% because of excluded rows
  correct check: 25.1% (AAPL) + 0% (TSLA) + 0% (MDB) + 60.4% (cash) = 85.5% ← STILL WRONG
  actual correct: all allocated_pct rows sum + cash should = 100%

The pre-computed ALLOCATION_SUM_TO_100 check already handles this correctly.
DO NOT re-derive your own allocation sum from invested_pct + cash_pct.
Trust the pre-computed check result — if it shows PASS, the allocation is valid.

You will receive:
- Pre-computed rule-based consistency checks (PASS/FAIL/WARN) — trust these
- Per-ticker cross-agent data: what each agent decided for each ticker
- Portfolio-level metadata including explicit allocation accounting fields

Your output MUST be a single valid JSON object with exactly these fields:

{
  "overall_status": "CLEAN" | "WARNINGS" | "VIOLATIONS",
  "audit_summary": "<2-3 sentence summary of what you found — focus on real issues, not accounting arithmetic>",
  "rule_check_results": [
    {
      "rule": "<rule name>",
      "status": "PASS" | "FAIL" | "WARN",
      "finding": "<1 sentence — what this means in plain English>"
    }
  ],
  "agent_consistency_issues": [
    {
      "issue_type": "SIGNAL_ALLOCATION_MISMATCH" | "CAP_VIOLATION" | "MISSING_TICKER" | "ANOMALY_IGNORED" | "STANCE_MISMATCH" | "OTHER",
      "severity": "LOW" | "MEDIUM" | "HIGH",
      "ticker": "<ticker or 'PORTFOLIO'>",
      "description": "<1-2 sentence description of the inconsistency>"
    }
  ],
  "signal_quality_assessment": "<1-2 sentences on overall signal consistency and agreement across the portfolio>",
  "recommendations": ["<actionable recommendation for next pipeline run>"],
  "pipeline_health_score": <integer 1-10, where 10 = perfectly clean pipeline>
}

Rules:
- Output ONLY the JSON. No preamble, no markdown fences.
- overall_status rules:
  - CLEAN: all pre-computed checks PASS, no agent_consistency_issues
  - WARNINGS: at least one WARN or LOW severity issue
  - VIOLATIONS: any FAIL check or MEDIUM/HIGH severity issue
- pipeline_health_score:
  - 10: all PASS, zero issues
  - 7-9: minor warnings only (e.g. CRITICAL market risk with appropriate defensive stance)
  - 4-6: at least one MEDIUM issue or multiple WARNs
  - 1-3: FAIL checks or HIGH severity issues
- agent_consistency_issues: only list issues BEYOND the pre-computed checks. Can be [].
- CRITICAL market risk level with high cash allocation and excluded tickers is CORRECT
  defensive behaviour — do NOT flag this as a violation. It should earn a higher health score.
- recommendations: concrete, specific, actionable.
- Be rigorous but fair. Expected defensive behaviour under CRITICAL risk is not a violation.
"""


def _build_prompt(ctx: dict) -> str:
    checks_lines = []
    for c in ctx["consistency_checks"]:
        checks_lines.append(
            f"  [{c['status']:4s}]  {c['rule']}\n"
            f"           {c['detail']}"
        )
    checks_block = "\n".join(checks_lines) if checks_lines else "  No pre-computed checks available."

    ticker_lines = []
    for t in ctx["ticker_cross"]:
        flags = ", ".join(t["risk_flags"]) if t["risk_flags"] else "none"
        ticker_lines.append(
            f"  {t['ticker']:6s}  analyst={t['analyst_signal']:4s} ({t['signal_strength']:8s})  "
            f"risk_score={t['risk_score']}  max_pos={t['max_position_pct']}%  "
            f"allocated={t['allocated_pct']:.1f}%  method={t['sizing_method']}\n"
            f"           flags=[{flags}]"
        )
    tickers_block = "\n".join(ticker_lines) if ticker_lines else "  No ticker data available."

    return f"""Audit the Orblex Industries pipeline cycle for {ctx['watchdog_date']}.

PIPELINE RUN IDs
----------------
Report run   : {ctx['report_run_id']}
Alloc run    : {ctx['alloc_run_id']}
Risk run     : {ctx['risk_run_id']}
Analyst run  : {ctx['analyst_run_id']}

PORTFOLIO-LEVEL METADATA
-------------------------
Market risk level              : {ctx['market_risk_level']}
Portfolio stance               : {ctx['portfolio_stance']}
Confidence level               : {ctx['confidence_level']}
Active tickers (>0% allocated) : {ctx['active_count']}
Excluded tickers (0% by design): {ctx['excluded_count']}
Invested (active tickers only) : {ctx['invested_pct']}%
Cash reserve                   : {ctx['cash_pct']}%
Ticker sum + cash (= 100 check): {ctx['allocation_total']}%
Flagged tickers                : {ctx['flagged_tickers'] or 'none'}
Report Writer notes            : {ctx['report_watchdog_notes'] or 'None.'}

ACCOUNTING NOTE: invested_pct ({ctx['invested_pct']}%) + cash ({ctx['cash_pct']}%) = {round(ctx['invested_pct'] + ctx['cash_pct'], 1)}%.
The gap from 100% is accounted for by {ctx['excluded_count']} excluded tickers holding 0%.
The ALLOCATION_SUM_TO_100 pre-computed check already validates the correct invariant.
Do NOT re-derive your own mismatch calculation from these two figures.

PRE-COMPUTED CONSISTENCY CHECKS (trust these)
----------------------------------------------
{checks_block}

PER-TICKER CROSS-AGENT VIEW
-----------------------------
{tickers_block}

Audit the pipeline and produce your watchdog report JSON now."""


def audit_pipeline(ctx: dict, api_key: Optional[str] = None) -> Optional[dict]:
    """
    Calls Claude Watchdog Agent and returns a parsed audit dict.
    Returns None on failure.
    """
    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_prompt(ctx)

    logger.info(f"  Calling Claude ({decision_model()}) for watchdog audit...")

    try:
        msg = ask("decision", SYSTEM_PROMPT, prompt, 3000, client)

        raw = msg.text.strip()

        # Strip markdown fences defensively
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
                    result = {}
            else:
                result = {}

        # Ensure required fields
        result.setdefault("overall_status",          "WARNINGS")
        result.setdefault("audit_summary",           "Audit completed.")
        result.setdefault("rule_check_results",      [])
        result.setdefault("agent_consistency_issues", [])
        result.setdefault("signal_quality_assessment", "")
        result.setdefault("recommendations",         [])
        result.setdefault("pipeline_health_score",   5)

        result["model"]         = msg.model
        result["prompt_tokens"] = msg.input_tokens
        result["output_tokens"] = msg.output_tokens

        logger.info(
            f"  Watchdog result: {result['overall_status']}  "
            f"health={result['pipeline_health_score']}/10  "
            f"issues={len(result['agent_consistency_issues'])}"
        )
        return result

    except Exception as exc:
        logger.error(f"  Watchdog agent failed: {exc}")
        return None