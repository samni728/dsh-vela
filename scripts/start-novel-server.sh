#!/bin/bash
# Start the DSH Novel sidecar with the workspace config, detached.
# The sidecar hosts the autorun orchestrator (daemon thread per project),
# which is the correct driver for long novel runs — NOT synchronous CLI calls.
#
# Usage:  ./start-novel-server.sh [--foreground]
set -euo pipefail

# Resolve code from this checkout. Novel data is configured separately and no
# longer needs to live next to the repository.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${DSH_NOVEL_REPO:-$(cd "$SCRIPT_DIR/.." && pwd)}"
USER_CONFIG="$HOME/.dsh-novel/config.yml"
LEGACY_CONFIG="$(dirname "$REPO_ROOT")/novel-config.yml"
if [ -n "${DSH_NOVEL_CONFIG:-}" ]; then
  CONFIG="$DSH_NOVEL_CONFIG"
elif [ -f "$USER_CONFIG" ]; then
  CONFIG="$USER_CONFIG"
else
  CONFIG="$LEGACY_CONFIG"
fi
VENV_PY="$REPO_ROOT/backend/.venv/bin/python3"
DASH_NOVEL="$REPO_ROOT/backend/.venv/bin/dsh-novel"
PORT="${DSH_NOVEL_PORT:-17861}"
LOG="${DSH_NOVEL_LOG:-$HOME/.dsh-novel/novel-server.log}"

if [ ! -f "$CONFIG" ]; then
  echo "ERROR: config not found: $CONFIG" >&2
  exit 1
fi

if [ ! -x "$VENV_PY" ] || [ ! -f "$DASH_NOVEL" ]; then
  echo "ERROR: Python environment is missing under $REPO_ROOT/backend/.venv" >&2
  echo "Rebuild it with: cd '$REPO_ROOT/backend' && uv sync --extra dev" >&2
  exit 1
fi

mkdir -p "$(dirname "$LOG")"

# Already healthy?
if curl -s -m 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "novel sidecar already running on :$PORT (config: $CONFIG)"
  exit 0
fi

export DSH_NOVEL_CONFIG="$CONFIG"

if [ "${1:-}" = "--foreground" ]; then
  exec "$VENV_PY" "$DASH_NOVEL" serve
fi

echo "starting novel sidecar on :$PORT (config: $CONFIG, log: $LOG)"
nohup "$VENV_PY" "$DASH_NOVEL" serve >>"$LOG" 2>&1 &

# Wait for health
for i in $(seq 1 30); do
  if curl -s -m 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "novel sidecar is up on :$PORT"
    exit 0
  fi
  sleep 0.5
done

echo "ERROR: sidecar did not become healthy on :$PORT within 15s (see $LOG)" >&2
exit 1
