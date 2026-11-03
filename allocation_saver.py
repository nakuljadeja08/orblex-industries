"""
allocation_saver.py — persists Portfolio Allocator output to Supabase.

Tables written:
  - allocation_plans    (one row per run)
  - allocation_details  (one row per ticker per run)
"""

import logging

from instrument_policy import sector

logger = logging.getLogger(__name__)


def save_allocation(
    client,
    run_id: str,
    ctx: dict,
    allocation: dict,
    dry_run: bool = False,
) -> bool:
    """
    Upserts the allocation plan and per-ticker details to Supabase.
    Returns True on success, False on failure.
    """
    # Build simple {ticker: pct} map for the JSONB column
    alloc_map = {
        a["ticker"]: a["allocated_pct"]
        for a in allocation.get("allocations", [])
    }

    invested_pct = sum(alloc_map.values())
    cash_pct     = allocation.get("cash_pct", round(100.0 - invested_pct, 2))

    plan_row = {
        "run_id":           run_id,
        "plan_date":        ctx["plan_date"],
        "analyst_run_id":   ctx["analyst_run_id"],
        "risk_run_id":      ctx["risk_run_id"],
        "total_tickers":    ctx["total_tickers"],
        "invested_pct":     round(invested_pct, 2),
        "cash_pct":         round(cash_pct, 2),
        "strategy_note":    allocation.get("strategy_note"),
        "allocations":      alloc_map,
        "model":            allocation.get("model"),
        "prompt_tokens":    allocation.get("prompt_tokens"),
        "output_tokens":    allocation.get("output_tokens"),
        "sector_exposure":  allocation.get("sector_exposure", {}),
        "theme_exposure":   allocation.get("theme_exposure", {}),
        # [] means the enforcer ran and found nothing to correct. NULL means it
        # never ran — the newsletter distinguishes the two, so don't default to [].
        "constraint_violations": allocation.get("constraint_violations"),
    }

    # Per-ticker detail rows
    ticker_map = {t["ticker"]: t for t in ctx["ticker_inputs"]}
    # Deterministic base weights the allocator handed the model (base ceiling
    # before model refinement). Kept for the dashboard decision trace.
    base_weights = allocation.get("base_weights") or {}
    detail_rows = []
    for a in allocation.get("allocations", []):
        ticker   = a["ticker"]
        t_ctx    = ticker_map.get(ticker, {})
        detail_rows.append({
            "plan_run_id":           run_id,
            "ticker":                ticker,
            "sector":                sector(ticker),
            "analyst_signal":        t_ctx.get("signal"),
            "signal_strength":       t_ctx.get("signal_strength"),
            "risk_score":            t_ctx.get("risk_score"),
            "max_position_pct":      t_ctx.get("max_position_pct"),
            "risk_flags":            t_ctx.get("risk_flags", []),
            "base_weight":           base_weights.get(ticker),
            "allocated_pct":         a.get("allocated_pct", 0.0),
            "allocation_rationale":  a.get("rationale"),
            "sizing_method":         a.get("sizing_method", "SIGNAL_WEIGHTED"),
        })

    if dry_run:
        logger.info("  [DRY RUN] allocation_plans row:")
        logger.info(f"    invested_pct  : {plan_row['invested_pct']}%")
        logger.info(f"    cash_pct      : {plan_row['cash_pct']}%")
        logger.info(f"    strategy_note : {plan_row['strategy_note']}")
        logger.info("  [DRY RUN] allocation_details:")
        for d in detail_rows:
            logger.info(
                f"    {d['ticker']:6s}  {d['allocated_pct']:5.1f}%  "
                f"method={d['sizing_method']}"
            )
        return True

    try:
        client.table("allocation_plans").upsert(
            plan_row, on_conflict="run_id"
        ).execute()
        logger.info("  Saved allocation_plans row.")

        if detail_rows:
            try:
                client.table("allocation_details").upsert(
                    detail_rows, on_conflict="plan_run_id,ticker"
                ).execute()
                logger.info(f"  Saved {len(detail_rows)} allocation_details rows.")
            except Exception as e:
                # The base_weight column (IMPROVEMENTS 3.2) is added by a manual
                # Supabase migration (agent_schema_v2.sql). If it hasn't been
                # applied yet, PostgREST rejects the whole write with PGRST204.
                # Rather than hard-fail the allocator — and cascade into the
                # report writer, watchdog, newsletter and backtest — drop the
                # one un-migrated column and retry. The dashboard's decision
                # trace shows "unknown" for base_weight until the migration
                # lands; everything else is preserved.
                msg = str(e)
                if "base_weight" in msg and ("PGRST204" in msg or "schema cache" in msg):
                    logger.warning(
                        "  allocation_details: 'base_weight' column not found — "
                        "retrying without it. Apply the base_weight migration in "
                        "agent_schema_v2.sql to enable the dashboard decision trace."
                    )
                    for d in detail_rows:
                        d.pop("base_weight", None)
                    client.table("allocation_details").upsert(
                        detail_rows, on_conflict="plan_run_id,ticker"
                    ).execute()
                    logger.info(
                        f"  Saved {len(detail_rows)} allocation_details rows "
                        "(without base_weight)."
                    )
                else:
                    raise

        return True

    except Exception as e:
        logger.error(f"  Supabase write error: {e}")
        return False
