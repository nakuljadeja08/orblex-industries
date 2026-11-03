"""
run_all.py — Orblex Industries Full Pipeline Orchestrator

FIXES:
  1. agent_args passes --run-id so analyst saves under shared pipeline run_id
  2. risk_args passes --analyst-run-id so risk reads CURRENT run's reports
  3. Stage 3.5 (drift) skipped in dry-run mode
"""

import os, sys, argparse, subprocess, datetime, uuid, time
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

# TRADING_MODE (and the rest of the config) arrives through .env in CI; the
# orchestrator reads it for Stage −1, children load it again via config.py.
load_dotenv()

# The orchestrator prints box-drawing characters, and the child stages emit model
# text full of ≤ / — etc. On a Windows console the default cp1252 encoder raises
# UnicodeEncodeError on the very first print. Force UTF-8 here AND export it so
# every subprocess this file spawns inherits it — belt and suspenders, so a run
# does not depend on each individual runner having its own guard.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

RESET = "\033[0m"; BOLD = "\033[1m"; AMBER = "\033[38;5;214m"
GREEN = "\033[38;5;71m"; RED = "\033[38;5;196m"; DIM = "\033[38;5;240m"; CYAN = "\033[38;5;38m"

def header(msg):
    print(f"\n{AMBER}{'─'*55}{RESET}\n{AMBER}{BOLD}  {msg}{RESET}\n{AMBER}{'─'*55}{RESET}")

def log(stage, msg, ok=True):
    icon = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
    print(f"  {icon}  {CYAN}[{stage}]{RESET} {msg}")

def run_script(script, extra_args=[], label=None):
    start = time.time()
    cmd   = [sys.executable, script] + extra_args
    try:
        result = subprocess.run(cmd, capture_output=False, text=True)
        return result.returncode == 0, time.time() - start
    except FileNotFoundError:
        return False, time.time() - start

def run_parallel(tasks, label):
    results = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {pool.submit(run_script, s, a, l): l for s, a, l in tasks}
        for fut in as_completed(futures):
            lbl = futures[fut]
            ok, dur = fut.result()
            results[lbl] = ok
            log(label, f"{lbl} → {'OK' if ok else 'FAILED'} ({dur:.1f}s)", ok)
    return results


# ── job_runs (the dashboard's AUTOMATION tab) ──────────────────────────────────
# Each pipeline run records itself like the trading jobs do, so a dispatch that
# GitHub never started shows up as "dispatched, never ran" instead of looking fine.
def _record_start(job, run_id, dry_run):
    try:
        from zoneinfo import ZoneInfo
        from supabase import create_client
        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        url = os.environ.get("GITHUB_SERVER_URL")
        gh = f"{url}/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ.get('GITHUB_RUN_ID')}" if url else None
        day = datetime.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        row = sb.table("job_runs").insert({"job": job, "run_date": day, "gh_run_url": gh,
                                           "details": {"run_id": run_id, "dry_run": dry_run}}).execute().data[0]
        return sb, row["id"]
    except Exception as e:                        # bookkeeping never stops the pipeline
        print(f"  {DIM}job_runs not recorded ({e}){RESET}")
        return None, None


def _record_finish(rec, stage_results, dry_run):
    sb, rid = rec
    if rid is None:
        return
    failed = [k for k, v in stage_results.items() if not v]
    status = "OK" if not failed else ("FAILED" if len(failed) == len(stage_results) else "WARN")
    summary = (f"{len(stage_results) - len(failed)}/{len(stage_results)} stages OK"
               + (f"; failed: {', '.join(failed)}" if failed else "") + (" (dry run)" if dry_run else ""))
    try:
        sb.table("job_runs").update({"status": status, "summary": summary,
                                     "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
                                    ).eq("id", rid).execute()
    except Exception as e:
        print(f"  {DIM}job_runs not updated ({e}){RESET}")


# The allocator, report and watchdog feed no order: the morning plan reads only the analyst,
# risk and regime rows. On a daily swing run they would spend Claude calls on tabs nobody trades
# from, so they run in the Saturday long-term run. An explicit --stages list still runs them.
WEEKLY_STAGES = {5, 6, 7}


def default_stages(longterm, trading_mode):
    stages = set(range(10)) | {35}
    return stages - WEEKLY_STAGES if not longterm and trading_mode == "swing" else stages


def main():
    parser = argparse.ArgumentParser(description="Orblex Industries — Full Pipeline Orchestrator")
    parser.add_argument("--dry-run",         action="store_true")
    parser.add_argument("--skip-newsletter", action="store_true")
    parser.add_argument("--tickers",         nargs="+")
    parser.add_argument("--run-id")
    parser.add_argument("--stages",          nargs="+", type=int)
    parser.add_argument("--macro-source",    choices=["bls", "fred"], default="bls")
    args = parser.parse_args()

    # The weekly long-term book (BOOK=longterm, position mode) gets its own run-id
    # prefix so "latest daily run" lookups (the dashboard) never pick it up.
    longterm   = os.getenv("BOOK", "daily").strip().lower() == "longterm"
    run_id     = args.run_id or f"{'longterm' if longterm else 'pipeline'}-{datetime.date.today().isoformat()}-{uuid.uuid4().hex[:6]}"
    dry_run    = args.dry_run
    all_stages = set(args.stages) if args.stages else default_stages(
        longterm, os.getenv("TRADING_MODE", "position").strip().lower())

    dry_flag     = ["--dry-run"] if dry_run else []
    tickers_flag = args.tickers or []

    # FIX 1: pass shared run_id to analyst so reports saved under pipeline run_id
    agent_args    = ["--run-id", run_id] + tickers_flag + dry_flag

    # In dry-run mode the analyst saves nothing to DB, so risk must fall back to latest real run.
    # In live mode, pin risk to the current pipeline run_id for consistency.
    risk_args     = dry_flag if dry_run else ["--analyst-run-id", run_id]

    run_id_args   = ["--run-id", run_id] + dry_flag
    pipeline_args = tickers_flag + dry_flag
    simple_args   = dry_flag

    print(f"\n{AMBER}{'═'*55}{RESET}")
    print(f"{AMBER}{BOLD}  ORBLEX INDUSTRIES — AUTONOMOUS PIPELINE{RESET}")
    print(f"{AMBER}{'═'*55}{RESET}")
    print(f"  {DIM}Run ID  : {run_id}{RESET}")
    print(f"  {DIM}Date    : {datetime.date.today()}{RESET}")
    print(f"  {DIM}Dry run : {dry_run}{RESET}")
    print(f"  {DIM}Stages  : {sorted(all_stages)}{RESET}")
    print(f"{AMBER}{'─'*55}{RESET}\n")

    pipeline_start = time.time()
    stage_results  = {}
    rec = _record_start("longterm_run" if longterm else "pipeline", run_id, dry_run)

    # Stage −1 — swing universe from the account balance (run_universe.py). The
    # chosen list is exported as TICKERS, which every later stage reads through
    # load_config. Any failure falls back to the full universe: that only costs
    # more Claude calls, it never stops the run or changes a trading rule.
    if os.getenv("TRADING_MODE", "position").strip().lower() == "swing" and not args.tickers and 0 in all_stages:
        header("Stage −1 — Universe from account balance")
        out_file = f".universe-{run_id}.txt"
        ok, dur = run_script("run_universe.py", ["--run-id", run_id, "--out", out_file] + dry_flag, "universe")
        chosen = ""
        if ok and os.path.exists(out_file):
            with open(out_file, encoding="utf-8") as f:
                chosen = f.read().strip()
            os.remove(out_file)
        if chosen:
            os.environ["TICKERS"] = chosen
            log("S-1", f"universe → {len(chosen.split(','))} tickers ({dur:.1f}s)", True)
        else:
            log("S-1", f"universe → FAILED, using the full universe ({dur:.1f}s)", False)
        stage_results["universe"] = bool(chosen)

    if 0 in all_stages:
        header("Stage 0 — Macro + Earnings + Smart Money Context (parallel)")
        tasks = [
                 ("run_macro.py",    run_id_args + ["--source", args.macro_source], "macro"),
                 ("run_earnings.py", run_id_args + (["--tickers"] + tickers_flag if tickers_flag else []), "earnings"),
                 ("run_regime.py",   run_id_args, "regime"),
                 ("run_fundamentals.py", run_id_args + (["--tickers"] + tickers_flag if tickers_flag else []), "fundamentals"),
                 ("run_smart_money.py", run_id_args, "smart_money"),
                ]
        r = run_parallel(tasks, "S0")
        stage_results["macro"]    = r.get("macro", False)
        stage_results["earnings"] = r.get("earnings", False)
        stage_results["regime"] = r.get("regime", False)
        stage_results["smart_money"] = r.get("smart_money", False)

    if 1 in all_stages or 2 in all_stages:
        header("Stage 1+2 — OHLCV Ingestion + Options Flow (parallel)")
        tasks = []
        if 1 in all_stages:
            tasks.append(("pipeline.py", pipeline_args, "pipeline"))
        if 2 in all_stages:
            tasks.append(("run_options.py", run_id_args + (["--tickers"] + tickers_flag if tickers_flag else []), "options"))
        r = run_parallel(tasks, "S1/2")
        stage_results.update(r)

    if 3 in all_stages:
        header("Stage 3 — Analyst Agent")
        ok, dur = run_script("run_agent.py", agent_args, "analyst")
        log("S3", f"analyst → {'OK' if ok else 'FAILED'} ({dur:.1f}s)", ok)
        stage_results["analyst"] = ok

    # Stage 3v2 — PIPELINE_V2 swing book in shadow mode (EXECUTOR_DESIGN.md phase 1):
    # writes only swing_v2_picks, so v1 keeps trading while both are logged. A
    # failure here is reported but never fails the run.
    if (3 in all_stages and os.getenv("PIPELINE_V2", "0").strip() == "1"
            and os.getenv("TRADING_MODE", "position").strip().lower() == "swing"):
        header("Stage 3v2 — Swing v2 (shadow)")
        ok, dur = run_script("run_swing_v2.py", ["--run-id", run_id] + dry_flag, "swing-v2")
        log("S3v2", f"swing v2 shadow → {'OK' if ok else 'FAILED (ignored)'} ({dur:.1f}s)", ok)

    # FIX 3: skip drift in dry-run (no data written to query)
    # Drift is skipped for the long-term book: its weekly position-mode calls would
    # otherwise read as "yesterday's call" to the next daily swing run.
    if longterm and (35 in all_stages or 3 in all_stages):
        log("S3.5", "drift → SKIPPED (long-term book)", ok=True)
    elif (35 in all_stages or 3 in all_stages) and not dry_run:
        header("Stage 3.5 — Signal Drift Tracking")
        ok, dur = run_script("run_drift.py", ["--run-id", run_id], "drift")
        log("S3.5", f"drift → {'OK' if ok else 'FAILED'} ({dur:.1f}s)", ok)
        stage_results["drift"] = ok
    elif dry_run:
        log("S3.5", "drift → SKIPPED (dry-run)", ok=True)

    if 4 in all_stages:
        header("Stage 4 — Risk Manager")
        ok, dur = run_script("run_risk.py", risk_args, "risk")
        log("S4", f"risk → {'OK' if ok else 'FAILED'} ({dur:.1f}s)", ok)
        stage_results["risk"] = ok

    if 5 in all_stages:
        header("Stage 5 — Portfolio Allocator")
        alloc_args = dry_flag if dry_run else (["--analyst-run-id", run_id] + dry_flag)
        ok, dur = run_script("run_allocator.py", alloc_args, "allocator")
        log("S5", f"allocator → {'OK' if ok else 'FAILED'} ({dur:.1f}s)", ok)
        stage_results["allocator"] = ok

        # Stage 5b — the week's advice for the main account (LONG TERM tab).
        if longterm and ok and not dry_run:
            header("Stage 5b — Long-term advice")
            ok, dur = run_script("build_longterm.py", ["--run-id", run_id], "longterm")
            log("S5b", f"long-term advice → {'OK' if ok else 'FAILED'} ({dur:.1f}s)", ok)
            stage_results["longterm"] = ok

    if 6 in all_stages:
        header("Stage 6 — Report Writer")
        ok, dur = run_script("run_report_writer.py", simple_args, "report")
        log("S6", f"report_writer → {'OK' if ok else 'FAILED'} ({dur:.1f}s)", ok)
        stage_results["report"] = ok

    if 7 in all_stages:
        header("Stage 7 — Pipeline Watchdog")
        ok, dur = run_script("run_watchdog.py", simple_args, "watchdog")
        log("S7", f"watchdog → {'OK' if ok else 'FAILED'} ({dur:.1f}s)", ok)
        stage_results["watchdog"] = ok

    if 8 in all_stages and not args.skip_newsletter:
        header("Stage 8 — Email Newsletter")
        ok, dur = run_script("run_newsletter.py", ["--run-id", run_id] + dry_flag, "newsletter")
        log("S8", f"newsletter → {'OK' if ok else 'FAILED'} ({dur:.1f}s)", ok)
        stage_results["newsletter"] = ok

    # Stage 9 runs last: the backtest re-scores every historical allocation_plan
    # against price outcomes that have since matured (1d/5d/20d). It reads only
    # allocation_plans + stock_prices, so it is independent of this run's run_id
    # and cannot block the newsletter. In dry-run it computes without writing.
    if 9 in all_stages:
        header("Stage 9 — Backtest (feedback loop)")
        ok, dur = run_script("run_backtest.py", simple_args + ["--summary"], "backtest")
        log("S9", f"backtest → {'OK' if ok else 'FAILED'} ({dur:.1f}s)", ok)
        stage_results["backtest"] = ok

    total_dur = time.time() - pipeline_start
    passed    = sum(1 for v in stage_results.values() if v)
    failed    = sum(1 for v in stage_results.values() if not v)

    print(f"\n{AMBER}{'═'*55}{RESET}")
    print(f"{AMBER}{BOLD}  PIPELINE COMPLETE{RESET}")
    print(f"{AMBER}{'─'*55}{RESET}")
    print(f"  {DIM}Run ID  : {run_id}{RESET}")
    print(f"  {DIM}Duration: {total_dur:.1f}s{RESET}")
    print(f"  {GREEN}Passed  : {passed}{RESET}")
    print(f"  {RED if failed else DIM}Failed  : {failed}{RESET}")
    for stage, ok in stage_results.items():
        icon = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        print(f"    {icon} {stage}")
    print(f"{AMBER}{'═'*55}{RESET}\n")

    _record_finish(rec, stage_results, dry_run)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()