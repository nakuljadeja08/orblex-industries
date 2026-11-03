"""
scheduler.py — Orblex Industries Daily Pipeline Scheduler

Runs run_all.py autonomously every trading day at 08:30 AM US/Eastern
(pre-market, before NYSE open at 09:30 AM).

Features:
  - Skips weekends and US market holidays automatically
  - Sends a startup log message on first run so you know it's alive
  - Supports SCHEDULER_DRY_RUN=true for safe testing
  - Graceful shutdown on SIGINT/SIGTERM

Setup:
    pip install apscheduler pytz pandas-market-calendars

Add to .env:
    SCHEDULER_DRY_RUN=false      # set true to test without running pipeline
    SCHEDULER_RUN_TIME=08:30     # 24h HH:MM in US/Eastern (default 08:30)

Run:
    python scheduler.py          # foreground, keeps running
    nohup python scheduler.py &  # background (simple Linux deploy)

For production, consider running via systemd or a process manager (pm2, supervisor).
A sample systemd unit file is printed at the bottom of this module.
"""

import logging
import os
import subprocess
import sys
from datetime import datetime, date

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────

# Model/report text contains non-cp1252 characters (≤, —). Without this the
# Windows console encoder raises UnicodeEncodeError inside the log handler.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scheduler.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

EASTERN_TZ   = pytz.timezone("US/Eastern")
RUN_TIME     = os.getenv("SCHEDULER_RUN_TIME", "08:30").strip()   # HH:MM Eastern
DRY_RUN      = os.getenv("SCHEDULER_DRY_RUN", "false").lower() == "true"
PYTHON       = sys.executable      # same python that's running the scheduler
RUN_ALL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_all.py")

try:
    RUN_HOUR, RUN_MINUTE = [int(x) for x in RUN_TIME.split(":")]
except ValueError:
    logger.error(f"Invalid SCHEDULER_RUN_TIME='{RUN_TIME}' — expected HH:MM. Defaulting to 08:30.")
    RUN_HOUR, RUN_MINUTE = 8, 30


# ── Market Holiday Check ──────────────────────────────────────────────────────

def _is_market_open_today() -> bool:
    """
    Returns True if today is a NYSE trading day (weekday + not a US market holiday).
    Uses pandas_market_calendars if available; falls back to simple weekday check.
    """
    today = date.today()

    # Fast path: weekends are never trading days
    if today.weekday() >= 5:
        logger.info(f"  Skipping — today is a weekend ({today.strftime('%A')})")
        return False

    # Use pandas_market_calendars for accurate holiday coverage
    try:
        import pandas_market_calendars as mcal
        nyse     = mcal.get_calendar("NYSE")
        schedule = nyse.schedule(
            start_date=today.strftime("%Y-%m-%d"),
            end_date=today.strftime("%Y-%m-%d"),
        )
        is_open = not schedule.empty
        if not is_open:
            logger.info(f"  Skipping — NYSE is closed today ({today}) — market holiday")
        return is_open

    except ImportError:
        # pandas_market_calendars not installed — log a warning and proceed
        logger.warning(
            "  pandas_market_calendars not installed — "
            "holiday detection disabled. "
            "Install with: pip install pandas-market-calendars"
        )
        return True   # assume open on weekdays if we can't check


# ── Pipeline Runner ───────────────────────────────────────────────────────────

def run_pipeline():
    """
    Called by APScheduler at the scheduled time.
    Checks if the market is open, then calls run_all.py as a subprocess.
    """
    now_et = datetime.now(EASTERN_TZ)
    logger.info("=" * 60)
    logger.info(f"Orblex Industries — Scheduled trigger fired")
    logger.info(f"Time (ET) : {now_et.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    logger.info(f"Dry run   : {DRY_RUN}")
    logger.info("=" * 60)

    if not _is_market_open_today():
        logger.info("  Pipeline skipped — not a trading day.")
        return

    # Build subprocess args
    cmd = [PYTHON, RUN_ALL_PATH]
    if DRY_RUN:
        cmd.append("--dry-run")

    logger.info(f"  Launching: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=False,       # let pipeline output go to stdout
            text=True,
            timeout=600,                # 10 min hard timeout — adjust if pipeline is slow
        )
        if result.returncode == 0:
            logger.info("  Pipeline completed successfully ✓")
        else:
            logger.error(f"  Pipeline exited with code {result.returncode} ✗")

    except subprocess.TimeoutExpired:
        logger.error("  Pipeline timed out after 10 minutes — check run_all.log")
    except Exception as exc:
        logger.error(f"  Failed to launch pipeline: {exc}")


# ── Scheduler Setup ───────────────────────────────────────────────────────────

def main():
    scheduler = BlockingScheduler(timezone=EASTERN_TZ)

    # Run Monday–Friday at the configured time (Eastern)
    trigger = CronTrigger(
        day_of_week="mon-fri",
        hour=RUN_HOUR,
        minute=RUN_MINUTE,
        timezone=EASTERN_TZ,
    )

    scheduler.add_job(
        run_pipeline,
        trigger=trigger,
        id="orblex_industries_daily",
        name="Orblex Industries Daily Pipeline",
        misfire_grace_time=300,         # allow 5 min late start before skipping
        coalesce=True,                  # run once even if multiple fires were missed
    )

    logger.info("=" * 60)
    logger.info("Orblex Industries — Scheduler starting")
    logger.info(f"Schedule  : Mon–Fri at {RUN_HOUR:02d}:{RUN_MINUTE:02d} US/Eastern")
    logger.info(f"Dry run   : {DRY_RUN}")
    logger.info(f"run_all   : {RUN_ALL_PATH}")
    logger.info("=" * 60)

    # Show when the next fire will be
    job      = scheduler.get_jobs()[0]
    next_run = job.next_run_time
    logger.info(f"Next run  : {next_run.strftime('%Y-%m-%d %H:%M:%S %Z') if next_run else 'N/A'}")
    logger.info("Scheduler running — Ctrl+C to stop\n")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


# ── Systemd Unit File (reference) ────────────────────────────────────────────
#
# Save as /etc/systemd/system/orblex-scheduler.service, then:
#   sudo systemctl daemon-reload
#   sudo systemctl enable orblex-scheduler
#   sudo systemctl start orblex-scheduler
#   sudo journalctl -u orblex-scheduler -f     # follow logs
#
# [Unit]
# Description=Orblex Industries Daily Pipeline Scheduler
# After=network.target
#
# [Service]
# Type=simple
# User=ubuntu
# WorkingDirectory=/home/ubuntu/portfolio-management
# ExecStart=/home/ubuntu/portfolio-management/.venv/bin/python scheduler.py
# Restart=always
# RestartSec=60
# StandardOutput=journal
# StandardError=journal
# EnvironmentFile=/home/ubuntu/portfolio-management/.env
#
# [Install]
# WantedBy=multi-user.target

if __name__ == "__main__":
    main()