"""
portfolio_stance.py — the report's portfolio stance, decided by rules.

The Report Writer's prompt states these rules, but the model labelled the stance
itself and broke them (e.g. CASH_HEAVY at 22.5% cash with 60% of tickers flagged,
which the rules make DEFENSIVE). run_report_writer.py now sets the stance from this
function after the model writes the prose, and watchdog_rules.py checks against the
same function, so the two can never disagree about what the rules say.

Most severe first:
  CASH_HEAVY   CRITICAL market risk OR cash > 25%
  DEFENSIVE    HIGH market risk OR more than 30% of tickers flagged
  AGGRESSIVE   majority BUY signals AND LOW market risk AND cash < 10%
  BALANCED     everything else
"""

from typing import Optional

LEVELS = ("LOW", "MODERATE", "HIGH", "CRITICAL")


def stance(market_risk_level: Optional[str], cash_pct, n_flagged: int, n_tickers: int,
           n_buy: int) -> Optional[str]:
    """None when the inputs are too incomplete to say."""
    try:
        cash = float(cash_pct)
    except (TypeError, ValueError):
        return None
    if market_risk_level not in LEVELS or n_tickers <= 0:
        return None
    if market_risk_level == "CRITICAL" or cash > 25:
        return "CASH_HEAVY"
    if market_risk_level == "HIGH" or n_flagged / n_tickers > 0.30:
        return "DEFENSIVE"
    if n_buy > n_tickers / 2 and market_risk_level == "LOW" and cash < 10:
        return "AGGRESSIVE"
    return "BALANCED"


def why(market_risk_level, cash_pct, n_flagged: int, n_tickers: int) -> str:
    return (f"market risk {market_risk_level}, cash {float(cash_pct):.1f}%, "
            f"{n_flagged} of {n_tickers} tickers flagged")
