#!/usr/bin/env python3
"""
Kalshi Scheduler
Runs the executor at 13:00 UTC and 20:00 UTC daily.  Uses explicit UTC time
comparisons (not the schedule library) so it is timezone-safe regardless of
the host's local timezone setting.

Drop a STOP file in the project root to pause scheduling.
"""

import logging
import sys
import time
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

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
# Configuration
# ---------------------------------------------------------------------------

STOP_FILE = config.ROOT_DIR / "STOP"

# Daily UTC run slots — (hour, minute).  Within TRIGGER_WINDOW_SECS of the
# scheduled time the executor will fire if it hasn't already run that slot today.
RUN_SLOTS_UTC: list[tuple[int, int]] = [
    (13, 0),   # 9:00 AM ET
    (20, 0),   # 4:00 PM ET
]
TRIGGER_WINDOW_SECS = 30   # fire if within ±30 s of the target time
STATUS_INTERVAL_SECS = 30 * 60  # print status every 30 minutes
LOOP_SLEEP_SECS = 5

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_state = {
    "runs_today": 0,
    "last_run": None,         # datetime (UTC)
    "today_date": None,       # date (UTC)
    "last_run_slot": None,    # "HH:MM" — which slot last fired today
    "last_status_print": 0.0, # time.monotonic() of last status print
}


def _reset_daily_if_needed(today_utc: date) -> None:
    if _state["today_date"] != today_utc:
        _state["runs_today"] = 0
        _state["today_date"] = today_utc
        _state["last_run_slot"] = None


# ---------------------------------------------------------------------------
# Core job
# ---------------------------------------------------------------------------

def run_executor() -> None:
    """Run the executor once, catching any exception so the scheduler stays alive."""
    if STOP_FILE.exists():
        log.warning("STOP file detected — skipping this run (remove STOP to resume)")
        return

    log.info("=== Executor run starting ===")
    try:
        executor_main()
        _state["runs_today"] += 1
        _state["last_run"] = datetime.now(timezone.utc)
        log.info("=== Executor run finished ===")
    except SystemExit as exc:
        _state["runs_today"] += 1
        _state["last_run"] = datetime.now(timezone.utc)
        log.info("=== Executor exited (code=%s) — treated as normal completion ===", exc.code)
    except Exception:
        log.error("=== Executor crashed — will retry next scheduled slot ===")
        log.error(traceback.format_exc())
        _state["last_run"] = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Status line
# ---------------------------------------------------------------------------

def print_status(now_utc: datetime) -> None:
    stop_note = "  [PAUSED — STOP file present]" if STOP_FILE.exists() else ""
    last = _state["last_run"]
    last_str = last.strftime("%H:%M:%S UTC") if last else "never"

    # Calculate next slot
    today_slots = sorted(RUN_SLOTS_UTC)
    now_hm = (now_utc.hour, now_utc.minute)
    upcoming = [s for s in today_slots if s > now_hm]
    if upcoming:
        h, m = upcoming[0]
        next_str = f"{h:02d}:{m:02d} UTC today"
    else:
        h, m = today_slots[0]
        next_str = f"{h:02d}:{m:02d} UTC tomorrow"

    print(
        f"[STATUS] last_run={last_str}  next_slot={next_str}"
        f"  runs_today={_state['runs_today']}{stop_note}"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Kalshi Scheduler starting (env=%s)", config.KALSHI_ENV)
    log.info(
        "Run slots (UTC): %s",
        ", ".join(f"{h:02d}:{m:02d}" for h, m in RUN_SLOTS_UTC),
    )
    log.info("STOP file : %s", STOP_FILE)

    # Run immediately on startup
    now_utc = datetime.now(timezone.utc)
    _reset_daily_if_needed(now_utc.date())
    run_executor()
    # Mark startup run against whichever slot is closest to now so we don't
    # double-fire if the bot starts within the trigger window of a slot.
    h, m = now_utc.hour, now_utc.minute
    for sh, sm in RUN_SLOTS_UTC:
        diff = abs((h * 60 + m) - (sh * 60 + sm))
        if diff <= (TRIGGER_WINDOW_SECS // 60) + 1:
            _state["last_run_slot"] = f"{sh:02d}:{sm:02d}"
            break

    _state["last_status_print"] = time.monotonic()
    log.info("Scheduler running — press Ctrl+C to stop")

    while True:
        time.sleep(LOOP_SLEEP_SECS)

        now_utc = datetime.now(timezone.utc)
        _reset_daily_if_needed(now_utc.date())

        # Check each UTC slot
        for slot_h, slot_m in RUN_SLOTS_UTC:
            slot_key = f"{slot_h:02d}:{slot_m:02d}"
            if _state["last_run_slot"] == slot_key:
                continue  # already fired this slot today

            # Seconds from now to this slot's target time today
            slot_secs = slot_h * 3600 + slot_m * 60
            now_secs  = now_utc.hour * 3600 + now_utc.minute * 60 + now_utc.second
            diff = abs(now_secs - slot_secs)

            if diff <= TRIGGER_WINDOW_SECS:
                log.info("Triggering scheduled run for slot %s UTC", slot_key)
                _state["last_run_slot"] = slot_key
                run_executor()
                break  # only fire one slot per loop iteration

        # Print status periodically
        if time.monotonic() - _state["last_status_print"] >= STATUS_INTERVAL_SECS:
            print_status(now_utc)
            _state["last_status_print"] = time.monotonic()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Scheduler stopped by user (Ctrl+C)")
        print("\nScheduler stopped.")
        sys.exit(0)
