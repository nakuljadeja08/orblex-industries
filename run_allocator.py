"""
run_allocator.py — Claude-powered Portfolio Allocator Agent.

Takes analyst signals + risk constraints and produces a final
portfolio allocation: what % of capital goes to each ticker.

Sizing logic (applied BEFORE the model, as hard constraints):
  - SELL signal OR risk_score >= 8 → excluded from portfolio (0%)
  - Regime-ineligible instruments (see instrument_policy) → excluded
  - Allocation capped at min(risk manager's cap, instrument class cap)
  - CRITICAL effective risk → max single position 10%, min 35% cash
  - Signal-weighted base allocation, risk-adjusted, then re-normalised after caps

The market regime's risk_floor can only ESCALATE the Risk Manager's level, never
reduce it — see _effective_risk_level(). After the model proposes an allocation,
enforce_allocation_constraints() clamps it in Python: the model PROPOSES, the
enforcer DISPOSES. Every correction is recorded in constraint_violations for the
Watchdog and the newsletter.
"""

import json
import logging
import re
from typing import Optional

import anthropic

from llm import ask

from instrument_policy import (
    is_eligible, position_cap, score_multiplier, enforce_allocation_constraints,
    compute_sector_exposure, compute_theme_exposure, SECTOR_CAPS, THEME_CAPS,
)

logger = logging.getLogger(__name__)

# Model and per-model request rules: llm.py.

# Ordered least → most severe. Index order is what makes max() meaningful.
RISK_ORDER = ["LOW", "MODERATE", "HIGH", "CRITICAL"]


def _effective_risk_level(market_risk_level: str, regime_floor: Optional[str]) -> str:
    """
    Combine the Risk Manager's level with the market regime's floor.

    The floor only ever escalates: a BULL regime (floor=None) leaves the Risk
    Manager's level untouched, while a CRITICAL Risk Manager level survives a
    BULL regime. max(), never min().
    """
    if not regime_floor or regime_floor not in RISK_ORDER:
        return market_risk_level
    if market_risk_level not in RISK_ORDER:
        return regime_floor
    return max(market_risk_level, regime_floor, key=RISK_ORDER.index)


# Cash reserve floors by effective risk level
CASH_FLOOR = {
    "LOW":      0.05,   # 5% cash
    "MODERATE": 0.10,   # 10% cash
    "HIGH":     0.20,   # 20% cash
    "CRITICAL": 0.35,   # 35% cash
}

SIGNAL_BASE = {
    "BUY":  1.0,
    "HOLD": 0.5,
    "SELL": 0.0,
}

STRENGTH_MULT = {
    "STRONG":   1.0,
    "MODERATE": 0.75,
    "WEAK":     0.5,
}

# Mechanical strength haircut for chronic flippers (run_drift.is_chronic): a name
# that flipped >=3 times in 7 days has an unstable signal, so we de-size it one
# notch DETERMINISTICALLY rather than asking the model to remember to be
# consistent (IMPROVEMENTS.md 2.1). This affects the sizing multiplier only — the
# analyst's actual signal_strength is left untouched.
STRENGTH_HAIRCUT = {
    "STRONG":   "MODERATE",
    "MODERATE": "WEAK",
    "WEAK":     "WEAK",   # already at the floor
}

CRITICAL_POSITION_CAP = 10.0

SYSTEM_PROMPT = """You are the Portfolio Allocator at Orblex Industries, an autonomous AI-powered portfolio management system.

Your job is to produce a final portfolio allocation — the exact percentage of capital to assign to each ticker.

You will receive:
- Market risk level, market regime, and the effective risk level derived from both
- Per-ticker data: analyst signal, signal strength, risk score (1-10), effective position cap, risk flags
- Pre-computed base weights (signal × strength weighted, already risk-capped and re-normalised)
- Cash reserve floor based on market conditions
- Sector and theme concentration caps

Your output MUST be a single valid JSON object with exactly these fields:

{
  "strategy_note": "<2-3 sentence explanation of the overall allocation strategy>",
  "cash_pct": <float: % held as cash, e.g. 12.5>,
  "allocations": [
    {
      "ticker": "AAPL",
      "allocated_pct": <float>,
      "sizing_method": "SIGNAL_WEIGHTED" | "RISK_CAPPED" | "EXCLUDED" | "MINIMUM_HOLD",
      "rationale": "<1 sentence>"
    }
  ]
}

CRITICAL RULES — violations will be clamped in Python and flagged by the Watchdog:
- sum(allocated_pct for all tickers) + cash_pct MUST equal exactly 100.0
- Never exceed a ticker's effective_cap (shown per ticker in the prompt — this
  already folds in the instrument class cap and the CRITICAL cap where they apply)
- Tickers shown with base_weight=0.00% and an exclusion reason are ineligible this
  regime. Leave them at 0.0 — do not reinstate them.
- Tickers marked CHRONIC_HAIRCUT have an unstable signal (repeated flips); their
  base_weight is already de-sized in Python. Do not size them above their
  base_weight — treat that as a ceiling, not a starting point.
- Tickers marked EARNINGS_IN=<n>d report within a few days and carry binary event
  risk; keep them at or below their base_weight and do not concentrate into them.
  Their per-ticker risk cap (max_position_pct) already reflects the Risk Manager's
  view — respect it as usual.
- Tickers with SELL signal must have allocated_pct = 0.0 and sizing_method = "EXCLUDED"
- Tickers with risk_score >= 8 must have allocated_pct = 0.0 and sizing_method = "EXCLUDED"
- Respect the cash_floor — do not go below it
- Respect the sector and theme caps shown in MARKET CONDITIONS
- CRITICAL effective risk: no single position > 10%
- sizing_method = "RISK_CAPPED" ONLY when the allocation equals effective_cap exactly
- When a ticker is EXCLUDED due to risk_score >= 8, its rationale field MUST state: "Excluded: risk_score=<score> >= threshold of 8."
- When the risk_score difference between an excluded ticker and the lowest-scored retained ticker is <= 1 point, note this boundary decision explicitly in strategy_note.
- Output ONLY the JSON. No preamble, no markdown fences.
- This is for simulation and research purposes only.
"""


def _effective_cap(ticker_input: dict, effective_risk: str) -> float:
    """
    Combined per-ticker cap: min(Risk Manager's cap, instrument class cap
    [, CRITICAL_POSITION_CAP when effective risk is CRITICAL]).

    Always the LOWER of the applicable limits — a CRITICAL cap of 10% must never
    widen a tighter per-ticker cap of 8%.
    """
    cap = position_cap(ticker_input["ticker"], float(ticker_input["max_position_pct"]))
    if effective_risk == "CRITICAL":
        cap = min(cap, CRITICAL_POSITION_CAP)
    return cap


def _compute_base_weights(ctx: dict) -> dict:
    """
    Pre-compute signal-weighted base allocations before passing to Claude.

    Regime eligibility and instrument class caps are applied here, so tickers the
    policy forbids in the current regime arrive at the model already zeroed.

    Re-normalises after capping so sum(base_allocs.values()) + cash_pct = 100.0
    exactly — without it, capped positions leave a gap the model cannot fill
    correctly.
    """
    effective_risk = _effective_risk_level(
        ctx["market_risk_level"], ctx.get("regime_risk_floor"))
    ctx["effective_risk_level"] = effective_risk   # downstream visibility

    cash_floor  = CASH_FLOOR.get(effective_risk, 0.10)
    investable  = 1.0 - cash_floor

    # ── Step 1: Score each ticker ─────────────────────────────────────────────
    scores          = {}
    ineligible_note = {}
    haircut_note    = {}   # ticker → (orig_strength, effective_strength)
    for t in ctx["ticker_inputs"]:
        signal   = t["signal"].upper()
        strength = t["signal_strength"].upper()

        if signal == "SELL" or t["risk_score"] >= 8:
            scores[t["ticker"]] = 0.0
            continue

        # Regime eligibility — defensive and inverse sleeves are gated by regime,
        # and inverse instruments additionally expire after a max holding period.
        eligible, reason = is_eligible(
            t["ticker"], ctx.get("regime"),
            (ctx.get("inverse_hold_days") or {}).get(t["ticker"], 0),
        )
        if not eligible:
            scores[t["ticker"]]          = 0.0
            ineligible_note[t["ticker"]] = reason
            continue

        # Mechanical chronic-flipper haircut: de-size an unstable signal one notch
        # for the multiplier only. STRONG/MODERATE lose weight; WEAK is unchanged.
        eff_strength = strength
        if t.get("is_chronic"):
            eff_strength = STRENGTH_HAIRCUT.get(strength, strength)
            if eff_strength != strength:
                haircut_note[t["ticker"]] = (strength, eff_strength)

        base         = SIGNAL_BASE.get(signal, 0.5)
        mult         = STRENGTH_MULT.get(eff_strength, 0.75)
        risk_penalty = 1.0 - ((t["risk_score"] - 1) / 9) * 0.5
        scores[t["ticker"]] = (
            base * mult * risk_penalty
            * score_multiplier(t["ticker"], ctx.get("regime"))
        )

    total_score = sum(scores.values())

    # ── Step 2: Normalise to investable budget ────────────────────────────────
    raw_allocs = {}
    if total_score > 0:
        for t in ctx["ticker_inputs"]:
            ticker = t["ticker"]
            weight = scores.get(ticker, 0.0) / total_score
            alloc  = round(weight * investable * 100, 4)
            raw_allocs[ticker] = min(alloc, _effective_cap(t, effective_risk))
    else:
        for t in ctx["ticker_inputs"]:
            raw_allocs[t["ticker"]] = 0.0

    # ── Step 3: RE-NORMALISE after capping ───────────────────────────────────
    # Capping reduces total invested below the investable budget. Scale eligible
    # (>0%) tickers back up so the sum fills the investable space.
    invested_after_caps = sum(raw_allocs.values())
    if invested_after_caps > 0 and invested_after_caps < (investable * 100):
        scale = (investable * 100) / invested_after_caps
        for ticker in raw_allocs:
            if raw_allocs[ticker] > 0:
                t   = next(x for x in ctx["ticker_inputs"] if x["ticker"] == ticker)
                cap = _effective_cap(t, effective_risk)
                raw_allocs[ticker] = min(round(raw_allocs[ticker] * scale, 4), cap)

    # ── Step 4: Final cash = 100 - sum(allocs) ───────────────────────────────
    invested = round(sum(raw_allocs.values()), 2)
    cash_pct = round(100.0 - invested, 2)

    # Ensure cash floor is respected — if scaling pushed cash below floor, clamp
    min_cash = round(cash_floor * 100, 2)
    if cash_pct < min_cash:
        # Trim positions proportionally to restore cash floor
        excess  = min_cash - cash_pct
        trimmed = 0.0
        for ticker in raw_allocs:
            if raw_allocs[ticker] > 0:
                trim = round(raw_allocs[ticker] / invested * excess, 4)
                raw_allocs[ticker] = max(0.0, raw_allocs[ticker] - trim)
                trimmed += trim
        cash_pct = round(100.0 - sum(raw_allocs.values()), 2)

    # Round to 2dp and do a final rounding correction on the largest position
    raw_allocs = {k: round(v, 2) for k, v in raw_allocs.items()}
    total      = sum(raw_allocs.values()) + cash_pct
    diff       = round(100.0 - total, 2)
    if diff != 0.0:
        # Apply rounding diff to the largest active position
        largest = max(raw_allocs, key=lambda k: raw_allocs[k], default=None)
        if largest:
            raw_allocs[largest] = round(raw_allocs[largest] + diff, 2)

    cash_pct = round(100.0 - sum(raw_allocs.values()), 2)

    logger.info(
        f"  Base weights: invested={sum(raw_allocs.values()):.2f}%  "
        f"cash={cash_pct:.2f}%  total={sum(raw_allocs.values()) + cash_pct:.2f}%"
    )
    if ineligible_note:
        logger.info(
            f"  Regime {ctx.get('regime')}: {len(ineligible_note)} ticker(s) "
            f"policy-excluded — "
            + ", ".join(f"{tk} ({rsn})" for tk, rsn in sorted(ineligible_note.items()))
        )
    if haircut_note:
        logger.info(
            f"  Chronic-flipper haircut applied to {len(haircut_note)} ticker(s) — "
            + ", ".join(f"{tk} ({a}→{b})" for tk, (a, b) in sorted(haircut_note.items()))
        )

    return {
        "base_allocs":     raw_allocs,
        "cash_pct":        cash_pct,
        "cash_floor_pct":  round(cash_floor * 100, 1),
        "effective_risk":  effective_risk,
        "ineligible":      ineligible_note,
        "haircut":         haircut_note,
    }


def _build_prompt(ctx: dict, base_weights: dict) -> str:
    ba      = base_weights["base_allocs"]
    cash    = base_weights["cash_pct"]
    c_floor = base_weights["cash_floor_pct"]

    effective_risk = base_weights.get(
        "effective_risk", ctx.get("effective_risk_level", ctx["market_risk_level"]))
    ineligible = base_weights.get("ineligible", {})
    haircut    = base_weights.get("haircut", {})

    ticker_lines = []
    for t in ctx["ticker_inputs"]:
        tk      = t["ticker"]
        flags   = ", ".join(t["risk_flags"]) if t["risk_flags"] else "none"
        eff_cap = _effective_cap(t, effective_risk)
        suffix  = f"  INELIGIBLE={ineligible[tk]}" if tk in ineligible else ""
        if tk in haircut:
            a, b = haircut[tk]
            suffix += f"  CHRONIC_HAIRCUT={a}->{b} (base_weight already reduced; do not restore)"
        dte = t.get("days_to_earnings")
        if dte is not None and dte <= 5:
            suffix += f"  EARNINGS_IN={dte}d (imminent — do not oversize)"
        ticker_lines.append(
            f"  {tk:6s}  signal={t['signal']:4s} ({t['signal_strength']:8s})  "
            f"risk_score={t['risk_score']:2d}  effective_cap={eff_cap:5.1f}%  "
            f"base_weight={ba.get(tk, 0.0):5.2f}%  flags=[{flags}]{suffix}"
        )

    tickers_block = "\n".join(ticker_lines)
    base_invested = sum(ba.values())

    sector_caps_line = "  ".join(f"{s} {v:.0f}%" for s, v in SECTOR_CAPS.items())
    theme_caps_line  = "  ".join(f"{t} {v:.0f}%" for t, v in THEME_CAPS.items())

    return f"""Produce the final portfolio allocation for {ctx['plan_date']}.

MARKET CONDITIONS
-----------------
Market risk level  : {ctx['market_risk_level']}  →  Effective risk: {effective_risk}
Market regime      : {ctx.get('regime') or 'n/a'}  (risk floor: {ctx.get('regime_risk_floor') or 'none'})
Concentration risk : {ctx['concentration_risk']}
Volatility risk    : {ctx['volatility_risk']}
Flagged tickers    : {ctx['flagged_tickers'] or 'none'}
Sector caps        : {sector_caps_line}
Theme caps         : {theme_caps_line}
Cash floor         : {c_floor}%
Pre-computed cash  : {cash}%

PER-TICKER INPUTS (never exceed effective_cap)
---------------------------------------------------
{tickers_block}

Base invested total: {base_invested:.2f}%  |  Pre-computed cash: {cash:.2f}%
Verification      : {base_invested:.2f}% + {cash:.2f}% = {base_invested + cash:.2f}% (must = 100.0)

The base weights above already sum to 100% with cash.
Refine them — you may adjust individual positions within their effective_cap
bounds, but any ticker marked INELIGIBLE stays at 0.0, and the final
sum(allocated_pct) + cash_pct MUST equal exactly 100.0.
Produce the allocation JSON now."""


def allocate(ctx: dict, api_key: Optional[str] = None) -> Optional[dict]:
    """
    Calls Claude Allocator Agent and returns a parsed allocation dict.
    Returns None on failure.
    """
    base_weights = _compute_base_weights(ctx)
    base_invested = sum(base_weights["base_allocs"].values())

    logger.info(
        f"  Base weights — "
        f"cash floor={base_weights['cash_floor_pct']}%  "
        f"invested={base_invested:.2f}%  "
        f"cash={base_weights['cash_pct']:.2f}%  "
        f"total={base_invested + base_weights['cash_pct']:.2f}%"
    )

    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_prompt(ctx, base_weights)

    logger.info("  Calling Claude Portfolio Allocator Agent...")

    try:
        response = ask("decision", SYSTEM_PROMPT, prompt, 8192, client)
    except Exception as e:
        logger.error(f"  Anthropic API error: {e}")
        return None

    if response.stop_reason == "max_tokens":
        logger.error(
            "  Allocator response hit max_tokens — JSON is truncated. "
            "Raise max_tokens or reduce per-ticker output."
        )
    raw = response.text.strip()

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
                logger.error(f"  Failed to parse allocator JSON. Raw:\n{raw[:300]}")
                return None
        else:
            logger.error(f"  No JSON found in allocator response. Raw:\n{raw[:300]}")
            return None

    # ── Post-process: enforce sum = 100 on Claude's output ───────────────────
    allocations = result.get("allocations", [])
    cash_out    = float(result.get("cash_pct", base_weights["cash_pct"]))
    total_out   = sum(float(a.get("allocated_pct", 0)) for a in allocations) + cash_out

    if abs(total_out - 100.0) > 0.5:
        logger.warning(
            f"  Claude allocation summed to {total_out:.2f}% — "
            f"falling back to pre-computed base weights"
        )
        # Fallback: use pre-computed base weights directly
        result["allocations"] = [
            {
                "ticker":        t["ticker"],
                "allocated_pct": base_weights["base_allocs"].get(t["ticker"], 0.0),
                "sizing_method": "EXCLUDED" if base_weights["base_allocs"].get(t["ticker"], 0.0) == 0.0 else "SIGNAL_WEIGHTED",
                "rationale":     f"Excluded: risk_score={t['risk_score']} >= threshold of 8."
                                 if t["risk_score"] >= 8
                                 else "Excluded: SELL signal." if t["signal"].upper() == "SELL"
                                 else f"Excluded: {base_weights['ineligible'][t['ticker']]} "
                                      f"in {ctx.get('regime')} regime."
                                 if t["ticker"] in base_weights.get("ineligible", {})
                                 else "Pre-computed base weight (fallback).",
            }
            for t in ctx["ticker_inputs"]
        ]
        result["cash_pct"] = base_weights["cash_pct"]

    # ── Post-LLM constraint enforcement ──────────────────────────────────────
    # The model PROPOSES; this pass DISPOSES. Exclusions, class caps, sector and
    # theme caps, the cash floor, and the sum-to-100 invariant are guaranteed in
    # Python regardless of what came back. Corrections are an audit artifact, not
    # an error — they are persisted and surfaced in the newsletter.
    result, violations = enforce_allocation_constraints(
        result,
        ctx["ticker_inputs"],
        effective_risk_level=base_weights["effective_risk"],
        cash_floor_pct=base_weights["cash_floor_pct"],
        regime=ctx.get("regime"),
        inverse_hold_days=ctx.get("inverse_hold_days"),
        critical_single_cap=CRITICAL_POSITION_CAP,
    )

    result["constraint_violations"] = violations
    result["sector_exposure"]       = compute_sector_exposure(result.get("allocations", []))
    result["theme_exposure"]        = compute_theme_exposure(result.get("allocations", []))
    result["effective_risk_level"]  = base_weights["effective_risk"]

    result["prompt_tokens"] = response.input_tokens
    result["output_tokens"] = response.output_tokens
    result["model"]         = response.model
    result["base_weights"]  = base_weights["base_allocs"]

    return result


# ── Runner entry point ────────────────────────────────────────────────────────

def _parse_args():
    import argparse
    parser = argparse.ArgumentParser(description="Orblex Industries Portfolio Allocator Agent")
    parser.add_argument("--analyst-run-id", default=None, help="Analyst run_id passed from run_all.py — used to locate the matching risk assessment")
    parser.add_argument("--risk-run-id",    default=None, help="Explicit risk run_id (overrides analyst-run-id lookup)")
    parser.add_argument("--dry-run", action="store_true", help="Print output, skip Supabase save")
    return parser.parse_args()


def run():
    import sys
    import uuid
    import logging
    from datetime import datetime
    from config import load_config
    from loader import get_client
    from allocation_context_builder import build_allocation_context
    from allocation_saver import save_allocation

    # Model output routinely contains non-cp1252 characters (≤, —, ≥). On Windows
    # the default console/file encoding raises UnicodeEncodeError inside the log
    # handler, which buries real output in tracebacks. Force UTF-8 on both.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("allocator_agent.log", encoding="utf-8"),
        ],
    )
    _logger = logging.getLogger(__name__)

    args   = _parse_args()
    run_id = f"alloc-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    try:
        cfg = load_config()
    except EnvironmentError as e:
        _logger.error(str(e))
        sys.exit(1)

    _logger.info("=" * 60)
    _logger.info("Orblex Industries — Portfolio Allocator Agent")
    _logger.info(f"Run ID   : {run_id}")
    _logger.info(f"Dry run  : {args.dry_run}")
    _logger.info("=" * 60)

    client = get_client(cfg.supabase_url, cfg.supabase_key)

    _logger.info("\n[1/3] Building allocation context from risk + analyst data...")
    _logger.info(f"  Tickers (.env)    : {', '.join(cfg.tickers)}")
    _logger.info(f"  Analyst run ID    : {args.analyst_run_id or 'not provided — using latest risk run'}")
    ctx = build_allocation_context(
        client,
        risk_run_id=args.risk_run_id,
        analyst_run_id=args.analyst_run_id,
        tickers=cfg.tickers,
    )
    if ctx is None:
        _logger.error("  No risk assessment found. Run run_risk.py first.")
        sys.exit(1)

    _logger.info(
        f"  Risk run       : {ctx['risk_run_id']}\n"
        f"  Analyst run    : {ctx['analyst_run_id']}\n"
        f"  Market risk    : {ctx['market_risk_level']}\n"
        f"  Tickers        : {', '.join(t['ticker'] for t in ctx['ticker_inputs'])}"
    )

    _logger.info("\n[2/3] Running Claude Portfolio Allocator Agent...")
    allocation = allocate(ctx, api_key=cfg.supabase_key and __import__("os").getenv("ANTHROPIC_API_KEY"))
    if allocation is None:
        _logger.error("  Allocator Agent failed. Check logs above.")
        sys.exit(1)

    invested   = sum(a["allocated_pct"] for a in allocation.get("allocations", []))
    violations = allocation.get("constraint_violations", [])
    _logger.info(
        f"\n  -- ALLOCATION RESULTS --\n"
        f"  Effective risk : {allocation.get('effective_risk_level', ctx['market_risk_level'])}"
        f"  (market={ctx['market_risk_level']}, regime={ctx.get('regime') or 'n/a'})\n"
        f"  Invested       : {invested:.1f}%\n"
        f"  Cash           : {allocation.get('cash_pct', 0):.1f}%\n"
        f"  Sector exposure: {allocation.get('sector_exposure', {})}\n"
        f"  Theme exposure : {allocation.get('theme_exposure', {})}\n"
        f"  Strategy       : {allocation.get('strategy_note', '')}\n"
        f"  Tokens         : {allocation.get('prompt_tokens', 0)} in / {allocation.get('output_tokens', 0)} out"
    )
    for a in allocation.get("allocations", []):
        _logger.info(f"    {a['ticker']:6s}  {a['allocated_pct']:5.1f}%  method={a['sizing_method']}")

    if violations:
        _logger.warning(f"\n  Constraint enforcer made {len(violations)} correction(s):")
        for v in violations:
            _logger.warning(f"    - {v}")
    else:
        _logger.info("\n  Constraint enforcer: model output was already compliant.")

    _logger.info(f"\n[3/3] {'Skipping Supabase save (dry-run)' if args.dry_run else 'Saving to Supabase...'}")
    success = save_allocation(client, run_id, ctx, allocation, dry_run=args.dry_run)

    if success:
        _logger.info("\n  Allocation complete.")
    else:
        _logger.error("\n  Failed to save allocation.")
        sys.exit(1)

    _logger.info("=" * 60)


if __name__ == "__main__":
    run()