#!/bin/bash
# Auto-refresh Purview for AI dashboard — runs daily at 7AM UTC
# Full refresh with AI scoring for all pages

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGFILE="${SCRIPT_DIR}/.purview-refresh.log"
SCRIPT="${SCRIPT_DIR}/purview-auto-refresh.py"

echo "=== Auto-refresh started at $(date -u '+%Y-%m-%d %H:%M:%S UTC') ===" >> "$LOGFILE"

# Run full refresh (all pages, with LLM scoring)
PYTHON_BIN="$(command -v python3 || command -v python)"
"$PYTHON_BIN" "$SCRIPT" >> "$LOGFILE" 2>&1

echo "=== Auto-refresh completed at $(date -u '+%Y-%m-%d %H:%M:%S UTC') ===" >> "$LOGFILE"
echo "" >> "$LOGFILE"
