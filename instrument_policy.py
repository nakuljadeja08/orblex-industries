"""
instrument_policy.py — Orblex Industries instrument classes & hard constraints.

Defines WHAT each ticker is allowed to do, and WHEN — enforced in Python,
never delegated to the model.

Instrument classes:
  CORE        Normal equities/ETFs. Eligible in every regime.
  DEFENSIVE   XLP / XLU / GLD. Eligible only in CORRECTION and BEAR regimes,
              where they receive a modest score boost. Hard-excluded otherwise.
  INVERSE     SH (-1x S&P 500). Daily-reset instrument subject to volatility
              decay — structurally unsuitable for buy-and-hold. Therefore:
                - eligible ONLY in BEAR regime
                - hard position cap (INVERSE_MAX_PCT), regardless of what
                  the Risk Manager or the model says
                - max consecutive holding period (INVERSE_MAX_HOLD_DAYS);
                  once exceeded, force-excluded until allocation drops to 0

This module also provides enforce_allocation_constraints(): a post-LLM
validation/clamp pass. The model PROPOSES an allocation; this function
DISPOSES — exclusions, caps, cash floor, and renormalization are guaranteed
in code, and every correction is recorded for the Watchdog / Decision Trace.

To enable the policy for a ticker, just add it to TICKERS in .env — class
membership is looked up here. Unknown tickers default to CORE.
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Instrument classes ────────────────────────────────────────────────────────

CORE      = "CORE"
DEFENSIVE = "DEFENSIVE"
INVERSE   = "INVERSE"

INSTRUMENT_CLASS = {
    # Defensive sleeve
    "XLP": DEFENSIVE,   # Consumer staples
    "XLU": DEFENSIVE,   # Utilities
    "GLD": DEFENSIVE,   # Gold
    # Inverse sleeve
    "SH":  INVERSE,     # -1x S&P 500 (daily reset)
}

# Regimes in which each class may hold a non-zero allocation.
# None covers "no regime row found" — only CORE trades blind.
ALL_REGIMES = {"BULL", "CHOPPY", "CORRECTION", "BEAR", None}

ELIGIBLE_REGIMES = {
    CORE:      ALL_REGIMES,
    DEFENSIVE: {"CORRECTION", "BEAR"},
    INVERSE:   {"BEAR"},
}

# ── Class-level hard caps & limits ───────────────────────────────────────────

INVERSE_MAX_PCT        = 5.0    # max % of portfolio in any INVERSE instrument
INVERSE_MAX_HOLD_DAYS  = 10     # max consecutive runs with non-zero allocation
DEFENSIVE_MAX_PCT      = 12.0   # per-ticker cap for DEFENSIVE instruments
DEFENSIVE_REGIME_BOOST = 1.25   # score multiplier when defensive & eligible

# sizing_method labels written to allocation_details
METHOD_REGIME_EXCLUDED = "REGIME_EXCLUDED"
METHOD_INVERSE_EXPIRED = "INVERSE_HOLD_EXPIRED"
METHOD_POLICY_CAPPED   = "POLICY_CAPPED"


# ── Sector / Theme mappings ────────────────────────────────────────────────────

TICKER_SECTOR: dict[str, str] = {
    # Semiconductors & chip design
    "NVDA": "SEMIS", "MU":   "SEMIS", "QCOM": "SEMIS", "ALAB": "SEMIS",
    "AVGO": "SEMIS", "NBIS": "SEMIS", "POET": "SEMIS",
    "WDC":  "SEMIS",   # Western Digital — storage / memory
    "ALGM": "SEMIS",   # Allegro MicroSystems — sensor ICs
    "UMC":  "SEMIS",   # United Microelectronics — foundry (small-account sleeve)
    # Enterprise software / SaaS / cloud infrastructure
    "MSFT": "SOFTWARE", "CRM":  "SOFTWARE", "ORCL": "SOFTWARE",
    "SNOW": "SOFTWARE", "DDOG": "SOFTWARE", "NOW":  "SOFTWARE",
    "PATH": "SOFTWARE", "AI":   "SOFTWARE", "S":    "SOFTWARE",   # small-account sleeve
    # Mega-cap platform tech
    "AAPL": "MEGA_CAP_TECH", "GOOG": "MEGA_CAP_TECH",
    "SCHG": "MEGA_CAP_TECH",   # Schwab US Large-Cap Growth ETF — mostly mega-cap tech
    # Networking / optical infrastructure
    "GLW": "INFRASTRUCTURE",
    # AI cloud infrastructure
    "CRWV": "INFRASTRUCTURE",   # CoreWeave — GPU cloud
    # Power generation / nuclear (AI-datacenter power demand)
    "VST": "POWER", "NNE": "POWER",
    "PCG": "POWER", "EXC": "POWER",   # utilities with datacenter load growth (small-account sleeve)
    # Robotics & warehouse automation
    "SYM": "ROBOTICS", "NOVT": "ROBOTICS",
    "ROBO": "ROBOTICS", "IBOT": "ROBOTICS",   # robotics/automation ETFs
}

SECTOR_THEME: dict[str, str] = {
    "SEMIS":            "TECH",
    "SOFTWARE":         "TECH",
    "MEGA_CAP_TECH":    "TECH",
    "INFRASTRUCTURE":   "TECH",
    "CLEAN_ENERGY":     "ENERGY",
    "POWER":            "ENERGY",
    "MATERIALS":        "INDUSTRIALS",
    "ROBOTICS":         "INDUSTRIALS",
    "AEROSPACE_DEFENSE":"INDUSTRIALS",
    "CONSUMER_STAPLES": "DEFENSIVE",
    "UTILITIES":        "DEFENSIVE",
    "COMMODITIES":      "DEFENSIVE",
    "INVERSE":          "SPECIAL",
}

# Hard concentration caps — enforced in Python, documented in the prompt
SECTOR_CAPS: dict[str, float] = {
    "SEMIS":    30.0,
    "SOFTWARE": 30.0,
}
THEME_CAPS: dict[str, float] = {
    "TECH": 80.0,
}


# ── Lookups ───────────────────────────────────────────────────────────────────

def instrument_class(ticker: str) -> str:
    return INSTRUMENT_CLASS.get(ticker.upper(), CORE)


def is_eligible(ticker: str, regime: Optional[str],
                inverse_hold_days: int = 0) -> tuple[bool, Optional[str]]:
    """
    Returns (eligible, reason_if_not).
    regime may be None if no market_regimes row exists yet.
    """
    cls = instrument_class(ticker)

    if regime not in ELIGIBLE_REGIMES[cls]:
        return False, METHOD_REGIME_EXCLUDED

    if cls == INVERSE and inverse_hold_days >= INVERSE_MAX_HOLD_DAYS:
        return False, METHOD_INVERSE_EXPIRED

    return True, None


def position_cap(ticker: str, risk_cap: float) -> float:
    """
    Effective per-ticker cap = min(Risk Manager's cap, class cap).
    Same min() principle as the CRITICAL_CAP lesson.
    """
    cls = instrument_class(ticker)
    if cls == INVERSE:
        return min(risk_cap, INVERSE_MAX_PCT)
    if cls == DEFENSIVE:
        return min(risk_cap, DEFENSIVE_MAX_PCT)
    return risk_cap


def score_multiplier(ticker: str, regime: Optional[str]) -> float:
    """Boost defensive instruments when (and only when) they are eligible."""
    if instrument_class(ticker) == DEFENSIVE and regime in ELIGIBLE_REGIMES[DEFENSIVE]:
        return DEFENSIVE_REGIME_BOOST
    return 1.0


CANDIDATES_FILE = Path(__file__).with_name("candidates.txt")
_candidate_sectors: Optional[dict] = None


def candidate_sectors(path: Path = CANDIDATES_FILE) -> dict[str, str]:
    """
    {TICKER: SECTOR} from candidates.txt — the sleeve pool the swing universe
    stage draws from. Lines are "TICKER SECTOR # comment"; a sector that is not
    a SECTOR_THEME key is ignored (that ticker would escape the caps).
    """
    global _candidate_sectors
    if _candidate_sectors is None or path != CANDIDATES_FILE:
        out: dict[str, str] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                parts = line.split("#", 1)[0].split()
                if len(parts) >= 2 and parts[1].upper() in SECTOR_THEME:
                    out[parts[0].upper()] = parts[1].upper()
                elif parts:
                    logger.warning(f"candidates.txt: skipping '{line.strip()}' — needs a known SECTOR")
        if path != CANDIDATES_FILE:
            return out
        _candidate_sectors = out
    return _candidate_sectors


def sector(ticker: str) -> str:
    t = ticker.upper()
    return TICKER_SECTOR.get(t) or candidate_sectors().get(t, "OTHER")


def theme(ticker: str) -> str:
    return SECTOR_THEME.get(sector(ticker), "OTHER")


def compute_sector_exposure(allocations: list[dict]) -> dict[str, float]:
    """Sum allocated_pct by sector across non-zero positions."""
    exp: dict[str, float] = {}
    for a in allocations:
        pct = float(a.get("allocated_pct", 0.0))
        if pct > 0:
            s = sector(a["ticker"])
            exp[s] = round(exp.get(s, 0.0) + pct, 2)
    return exp


def compute_theme_exposure(allocations: list[dict]) -> dict[str, float]:
    """Sum allocated_pct by theme across non-zero positions."""
    exp: dict[str, float] = {}
    for a in allocations:
        pct = float(a.get("allocated_pct", 0.0))
        if pct > 0:
            th = theme(a["ticker"])
            exp[th] = round(exp.get(th, 0.0) + pct, 2)
    return exp


# ── Post-LLM constraint enforcement ───────────────────────────────────────────

def enforce_allocation_constraints(
    allocation: dict,
    ticker_inputs: list[dict],
    effective_risk_level: str,
    cash_floor_pct: float,
    regime: Optional[str],
    inverse_hold_days: dict[str, int] | None = None,
    critical_single_cap: float = 10.0,
) -> tuple[dict, list[str]]:
    """
    Validate and clamp the model's proposed allocation IN PLACE of trusting it.

    Guarantees, in order:
      1. SELL signals and risk_score >= 8         → 0% (EXCLUDED)
      2. Regime-ineligible / hold-expired tickers → 0% (REGIME_EXCLUDED /
                                                        INVERSE_HOLD_EXPIRED)
      3. Per-ticker cap = min(max_position_pct, class cap
                              [, critical_single_cap if CRITICAL])
      4. cash_pct >= cash_floor_pct (scale invested down proportionally if not)
      5. allocations + cash sum to exactly 100.0 (cash absorbs the residual)

    Returns (clamped_allocation, violations) where violations is a list of
    human-readable corrections made — empty list means the model's output was
    already fully compliant. Violations are an audit artifact, not an error.
    """
    inverse_hold_days = inverse_hold_days or {}
    violations: list[str] = []

    inputs_by_ticker = {t["ticker"]: t for t in ticker_inputs}
    allocs = {a["ticker"]: a for a in allocation.get("allocations", [])}

    # Tickers the model silently dropped get explicit 0% rows (auditability)
    for tk in inputs_by_ticker:
        if tk not in allocs:
            violations.append(f"{tk}: missing from model output — added at 0%")
            allocs[tk] = {"ticker": tk, "allocated_pct": 0.0,
                          "sizing_method": "EXCLUDED",
                          "rationale": "Omitted by model; added by enforcer."}

    for tk, a in allocs.items():
        t   = inputs_by_ticker.get(tk, {})
        pct = float(a.get("allocated_pct", 0.0))

        # 1. Signal / risk exclusions
        if t.get("signal", "").upper() == "SELL" or int(t.get("risk_score", 5)) >= 8:
            if pct > 0:
                violations.append(f"{tk}: {pct:.1f}% on SELL/risk>=8 — forced to 0%")
            a["allocated_pct"] = 0.0
            a["sizing_method"] = "EXCLUDED"
            continue

        # 2. Regime eligibility / inverse holding period
        ok, reason = is_eligible(tk, regime, inverse_hold_days.get(tk, 0))
        if not ok:
            if pct > 0:
                violations.append(f"{tk}: {pct:.1f}% but {reason} in {regime} — forced to 0%")
            a["allocated_pct"] = 0.0
            a["sizing_method"] = reason
            continue

        # 3. Effective cap
        cap = position_cap(tk, float(t.get("max_position_pct", 15.0)))
        if effective_risk_level == "CRITICAL":
            cap = min(cap, critical_single_cap)
        if pct > cap:
            violations.append(f"{tk}: {pct:.1f}% exceeds cap {cap:.1f}% — clamped")
            a["allocated_pct"] = round(cap, 2)
            a["sizing_method"] = METHOD_POLICY_CAPPED

    # 4. Sector caps
    for sec, cap in SECTOR_CAPS.items():
        in_sec   = [tk for tk, a in allocs.items()
                    if sector(tk) == sec and float(a.get("allocated_pct", 0)) > 0]
        sec_total = sum(float(allocs[tk]["allocated_pct"]) for tk in in_sec)
        if sec_total > cap:
            scale = cap / sec_total
            violations.append(
                f"Sector {sec}: {sec_total:.1f}% > cap {cap:.1f}% — "
                f"scaled by {scale:.3f}"
            )
            for tk in in_sec:
                allocs[tk]["allocated_pct"] = round(
                    float(allocs[tk]["allocated_pct"]) * scale, 2)
                if allocs[tk].get("sizing_method") not in (
                    "EXCLUDED", METHOD_REGIME_EXCLUDED, METHOD_INVERSE_EXPIRED
                ):
                    allocs[tk]["sizing_method"] = METHOD_POLICY_CAPPED

    # 5. Theme caps
    for thm, cap in THEME_CAPS.items():
        in_thm    = [tk for tk, a in allocs.items()
                     if theme(tk) == thm and float(a.get("allocated_pct", 0)) > 0]
        thm_total = sum(float(allocs[tk]["allocated_pct"]) for tk in in_thm)
        if thm_total > cap:
            scale = cap / thm_total
            violations.append(
                f"Theme {thm}: {thm_total:.1f}% > cap {cap:.1f}% — "
                f"scaled by {scale:.3f}"
            )
            for tk in in_thm:
                allocs[tk]["allocated_pct"] = round(
                    float(allocs[tk]["allocated_pct"]) * scale, 2)
                if allocs[tk].get("sizing_method") not in (
                    "EXCLUDED", METHOD_REGIME_EXCLUDED, METHOD_INVERSE_EXPIRED
                ):
                    allocs[tk]["sizing_method"] = METHOD_POLICY_CAPPED

    # 6. Cash floor
    invested = sum(a["allocated_pct"] for a in allocs.values())
    cash     = round(100.0 - invested, 2)
    if cash < cash_floor_pct:
        scale = (100.0 - cash_floor_pct) / invested if invested > 0 else 0.0
        violations.append(
            f"cash {cash:.1f}% below floor {cash_floor_pct:.1f}% — "
            f"invested scaled by {scale:.3f}"
        )
        for a in allocs.values():
            a["allocated_pct"] = round(a["allocated_pct"] * scale, 2)
        invested = sum(a["allocated_pct"] for a in allocs.values())
        cash     = round(100.0 - invested, 2)

    # 7. Exact 100.0 — cash absorbs rounding residue
    allocation["allocations"] = list(allocs.values())
    allocation["cash_pct"]    = cash

    if violations:
        logger.warning("  Constraint enforcer corrected %d issue(s):", len(violations))
        for v in violations:
            logger.warning(f"    - {v}")

    return allocation, violations