#!/usr/bin/env bash
# run_bot.sh — start the Kalshi trading bot scheduler.
# Double-click in Finder or run from terminal: bash run_bot.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Python environment — prefer a local venv, fall back to system python3
# ---------------------------------------------------------------------------
if [ -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/.venv/bin/activate"
    echo "[run_bot] Using venv: $SCRIPT_DIR/.venv"
elif [ -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/venv/bin/activate"
    echo "[run_bot] Using venv: $SCRIPT_DIR/venv"
else
    echo "[run_bot] No venv found — using system python3 ($(which python3))"
fi

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "[run_bot] ERROR: .env file not found at $SCRIPT_DIR/.env"
    exit 1
fi

if [ -f "$SCRIPT_DIR/STOP" ]; then
    echo "[run_bot] WARNING: STOP file is present — scheduler will pause after each check."
    echo "          Remove it with:  rm $SCRIPT_DIR/STOP"
fi

# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------
echo "[run_bot] Starting scheduler at $(date)"
echo "[run_bot] Logs → $SCRIPT_DIR/data/logs/scheduler.log"
echo "[run_bot] Press Ctrl+C to stop."
echo ""

exec python3 "$SCRIPT_DIR/scripts/scheduler.py"
