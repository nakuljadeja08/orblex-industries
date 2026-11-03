"""
metrics_saver.py — pipeline execution metrics helper.

Writes stage-level timing and token data to the pipeline_metrics table.
Imported by run_all.py; all errors are swallowed so a metrics failure
never crashes the main pipeline.

Stage → Supabase table mapping:
  macro       → macro_snapshots   (run_id filter)
  earnings    → earnings_calendar (run_id filter)
  pipeline    → no LLM; token_cost = 0
  options     → options_flow      (run_id filter)
  analyst     → agent_reports     (run_id filter)
  risk        → risk_assessments  (latest row)
  allocator   → allocation_plans  (latest row)
  report      → final_reports     (latest row)
  watchdog    → watchdog_logs     (latest row)
"""

import os
from datetime import date
from typing import Optional

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

_TABLE = "pipeline_metrics"

# (supabase_table, run_id_column_or_None, date_column_for_latest_query)
_STAGE_MAP: dict[str, tuple[str, Optional[str], str]] = {
    "macro":     ("macro_snapshots",   "run_id",  "snapshot_date"),
    "earnings":  ("earnings_calendar", "run_id",  "fetch_date"),
    "options":   ("options_flow",      "run_id",  "snapshot_date"),
    "analyst":   ("agent_reports",     "run_id",  "report_date"),
    "risk":      ("risk_assessments",  None,      "assessment_date"),
    "allocator": ("allocation_plans",  None,      "plan_date"),
    "report":    ("final_reports",     None,      "report_date"),
    "watchdog":  ("watchdog_logs",     None,      "created_at"),
}


def _client() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def _fetch_tokens(client: Client, pipeline_run_id: str, stage_name: str) -> Optional[int]:
    """Return total tokens for a just-completed stage. Returns None on failure."""
    if stage_name == "pipeline":
        return 0  # OHLCV ingest — no LLM calls
    if stage_name not in _STAGE_MAP:
        return None

    table, run_id_col, date_col = _STAGE_MAP[stage_name]
    try:
        if run_id_col:
            # Stages 0-3 write with the shared pipeline run_id
            resp = (
                client.table(table)
                .select("prompt_tokens,output_tokens")
                .eq(run_id_col, pipeline_run_id)
                .execute()
            )
        else:
            # Risk/allocator/report/watchdog auto-generate their own run_ids;
            # safe to use latest row since we call this right after the stage.
            resp = (
                client.table(table)
                .select("prompt_tokens,output_tokens")
                .order(date_col, desc=True)
                .limit(1)
                .execute()
            )
        if resp.data:
            return sum(
                (r.get("prompt_tokens") or 0) + (r.get("output_tokens") or 0)
                for r in resp.data
            )
    except Exception:
        pass
    return None


def save_stage_metric(
    pipeline_run_id: str,
    stage_name: str,
    status: str,           # "ok" | "failed" | "skipped"
    duration_seconds: float,
) -> None:
    """Upsert one pipeline_metrics row. Never raises."""
    try:
        client = _client()
        token_cost = _fetch_tokens(client, pipeline_run_id, stage_name)
        client.table(_TABLE).upsert(
            {
                "pipeline_run_id":  pipeline_run_id,
                "run_date":         date.today().isoformat(),
                "stage_name":       stage_name,
                "status":           status,
                "duration_seconds": round(float(duration_seconds), 2),
                "token_cost":       token_cost,
            },
            on_conflict="pipeline_run_id,stage_name",
        ).execute()
    except Exception as e:
        print(f"[metrics] warning: could not save '{stage_name}' metric: {e}")
