"""
watchdog_saver.py — persists Watchdog Agent output to Supabase.

Tables written:
  - watchdog_logs    (one row per run — full audit record)
  - watchdog_issues  (one row per issue found per run)
"""

import logging

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}


def save_watchdog_audit(
    client,
    run_id: str,
    ctx: dict,
    audit: dict,
    dry_run: bool = False,
) -> bool:
    """
    Upserts the watchdog audit and any flagged issues to Supabase.
    Returns True on success, False on failure.
    """
    log_row = {
        "run_id":                    run_id,
        "watchdog_date":             ctx["watchdog_date"],
        "report_run_id":             ctx["report_run_id"],
        "alloc_run_id":              ctx["alloc_run_id"],
        "risk_run_id":               ctx["risk_run_id"],
        "analyst_run_id":            ctx["analyst_run_id"],
        "overall_status":            audit.get("overall_status", "UNKNOWN"),
        "pipeline_health_score":     audit.get("pipeline_health_score"),
        "audit_summary":             audit.get("audit_summary"),
        "signal_quality_assessment": audit.get("signal_quality_assessment"),
        "rule_check_results":        audit.get("rule_check_results", []),
        "recommendations":           audit.get("recommendations", []),
        "model":                     audit.get("model"),
        "prompt_tokens":             audit.get("prompt_tokens"),
        "output_tokens":             audit.get("output_tokens"),
    }

    # Per-issue rows, one per (ticker, issue_type). The model can report the same
    # pair twice, and Postgres rejects an upsert batch that hits a key twice, so
    # duplicates are merged: highest severity wins, distinct descriptions are joined.
    issues_by_key = {}
    for issue in audit.get("agent_consistency_issues", []):
        row = {
            "watchdog_run_id": run_id,
            "issue_type":      issue.get("issue_type"),
            "severity":        issue.get("severity"),
            "ticker":          issue.get("ticker") or "PORTFOLIO",
            "description":     issue.get("description"),
        }
        key = (row["ticker"], row["issue_type"])
        existing = issues_by_key.get(key)
        if existing is None:
            issues_by_key[key] = row
            continue
        if _SEVERITY_RANK.get(row["severity"], 0) > _SEVERITY_RANK.get(existing["severity"], 0):
            existing["severity"] = row["severity"]
        seen = existing["description"].split(" | ") if existing["description"] else []
        if row["description"] and row["description"] not in seen:
            existing["description"] = " | ".join(seen + [row["description"]])
    issue_rows = list(issues_by_key.values())

    if dry_run:
        logger.info("  [DRY RUN] watchdog_logs row:")
        logger.info(f"    overall_status         : {log_row['overall_status']}")
        logger.info(f"    pipeline_health_score  : {log_row['pipeline_health_score']}")
        logger.info(f"    audit_summary          : {log_row['audit_summary'][:120] if log_row['audit_summary'] else 'n/a'}...")
        if issue_rows:
            logger.info(f"  [DRY RUN] {len(issue_rows)} watchdog_issues rows:")
            for i in issue_rows:
                logger.info(f"    [{i['severity']}] {i['issue_type']} — {i['ticker']}: {i['description'][:80] if i['description'] else ''}")
        else:
            logger.info("  [DRY RUN] No issues to write.")
        return True

    try:
        client.table("watchdog_logs").upsert(
            log_row, on_conflict="run_id"
        ).execute()
        logger.info("  Saved watchdog_logs row.")

        if issue_rows:
            client.table("watchdog_issues").upsert(
                issue_rows, on_conflict="watchdog_run_id,ticker,issue_type"
            ).execute()
            logger.info(f"  Saved {len(issue_rows)} watchdog_issues rows.")
        else:
            logger.info("  No issues to persist — pipeline is clean.")

        return True

    except Exception as e:
        logger.error(f"  Supabase write error: {e}")
        return False
