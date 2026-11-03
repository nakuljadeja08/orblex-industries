"""
regime_saver.py — persists market regime detections to Supabase.

Follows the same saver convention as risk_saver / allocation_saver:
takes the client, run_id, and result dict; honors dry_run.
"""

import logging

logger = logging.getLogger(__name__)

TABLE = "market_regimes"


def save_regime(client, run_id: str, result: dict, dry_run: bool = False) -> bool:
    """
    Insert one market_regimes row. Returns True on success.
    """
    row = {
        "run_id":            run_id,
        "regime_date":       result["regime_date"],
        "regime":            result["regime"],
        "risk_floor":        result["risk_floor"],
        "spy_close":         result["spy_close"],
        "spy_ma50":          result["spy_ma50"],
        "spy_ma200":         result["spy_ma200"],
        "spy_vs_ma200_pct":  result["spy_vs_ma200_pct"],
        "spy_drawdown_pct":  result["spy_drawdown_pct"],
        "death_cross":       result["death_cross"],
        "qqq_vs_ma200_pct":  result["qqq_vs_ma200_pct"],
        "vix_close":         result["vix_close"],
        "rationale":         result["rationale"],
        "rules_fired":       result["rules_fired"],
    }

    if dry_run:
        logger.info(f"  [DRY RUN] Would insert into {TABLE}: regime={row['regime']}, "
                    f"risk_floor={row['risk_floor']}")
        return True

    try:
        client.table(TABLE).insert(row).execute()
        logger.info(f"  Saved regime row: {row['regime']} (run_id={run_id})")
        return True
    except Exception as e:
        logger.error(f"  Failed to save regime: {e}")
        return False