#!/usr/bin/env python3
"""
Kalshi Scheduler
Runs the executor every 60 minutes, logs each run, and prints a status line
every 10 minutes.  Drop a STOP file in the project root to pause scheduling.
"""

import logging
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

import schedule

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from scripts.executor import main as executor_main

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log_dir = config.LOG_DIR
_log_dir.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("scheduler")
log.setLevel(logging.INFO)
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    _fh = logging.FileHandler(_log_dir / "scheduler.log")
    _fh.setFormatter(_fmt)
    log.addHandler(_sh)
    log.addHandler(_fh)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

STOP_FILE = config.ROOT_DIR / "STOP"

_state = {
    "runs_today": 0,
    "last_run": None,       # datetime (UTC)
    "next_run": None,       # datetime (UTC)
    "today_date": date.today(),
}


def _reset_daily_count_if_needed() -> None:
    today = date.today()
    if _state["today_date"] != today:
        _state["runs_today"] = 0
        _state["today_date"] = today


# ---------------------------------------------------------------------------
# Core job
# ---------------------------------------------------------------------------

def run_executor() -> None:
    """Scheduled job: run the executor once, catching any exception."""
    _reset_daily_count_if_needed()

    if STOP_FILE.exists():
        log.warning("STOP file detected — skipping this run (remove STOP to resume)")
        return

    now = datetime.now(timezone.utc)
    log.info("=== Executor run starting ===")

    try:
        executor_main()
        _state["runs_today"] += 1
        _state["last_run"] = datetime.now(timezone.utc)
        log.info("=== Executor run finished ===")
    except SystemExit as exc:
        # executor calls sys.exit() in some paths — treat as normal completion
        _state["runs_today"] += 1
        _state["last_run"] = datetime.now(timezone.utc)
        log.info("=== Executor exited (code=%s) — treated as normal completion ===", exc.code)
    except Exception:
        log.error("=== Executor crashed — will retry next scheduled run ===")
        log.error(traceback.format_exc())
        # Still count so last_run updates; next run is unaffected
        _state["last_run"] = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Status line
# ---------------------------------------------------------------------------

def print_status() -> None:
    """Print a one-line status summary every 10 minutes."""
    _reset_daily_count_if_needed()

    if STOP_FILE.exists():
        stop_note = "  [PAUSED — STOP file present]"
    else:
        stop_note = ""

    last = _state["last_run"]
    last_str = last.strftime("%H:%M:%S UTC") if last else "never"

    next_job = next(
        (j for j in schedule.jobs if getattr(j, "job_func", None) is not None
         and j.job_func.func is run_executor),
        None,
    )
    if next_job and next_job.next_run:
        next_str = next_job.next_run.strftime("%H:%M:%S")
    else:
        next_str = "unknown"

    print(
        f"[STATUS] last_run={last_str}  next_run={next_str}"
        f"  runs_today={_state['runs_today']}{stop_note}"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Kalshi Scheduler starting (env=%s)", config.KALSHI_ENV)
    log.info("Run interval : every 60 minutes")
    log.info("Status line  : every 10 minutes")
    log.info("STOP file    : %s", STOP_FILE)

    # Run immediately on start, then on the 60-minute cadence
    run_executor()
    _state["next_run"] = datetime.now(timezone.utc)

    schedule.every(60).minutes.do(run_executor)
    schedule.every(10).minutes.do(print_status)

    log.info("Scheduler running — press Ctrl+C to stop")

    import time
    while True:
        schedule.run_pending()
        time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Scheduler stopped by user (Ctrl+C)")
        print("\nScheduler stopped.")
        sys.exit(0)
