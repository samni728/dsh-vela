#!/bin/bash
# Start the DSH Novel sidecar with the workspace config, detached.
# The sidecar hosts the autorun orchestrator (daemon thread per project),
# which is the correct driver for long novel runs — NOT synchronous CLI calls.
#
# Usage:  ./start-novel-server.sh [--foreground]
set -euo pipefail

# Script may live at workspace root or under dsh-vela/scripts/: walk up until
# we find the dir that contains both novel-data/ and dsh-vela/.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="${DSH_NOVEL_WORKSPACE:-$SCRIPT_DIR}"
while [ ! -d "$WORKSPACE/novel-data" ] || [ ! -d "$WORKSPACE/dsh-vela" ]; do
  PARENT="$(dirname "$WORKSPACE")"
  [ "$PARENT" = "$WORKSPACE" ] && break
  WORKSPACE="$PARENT"
done
CONFIG="${DSH_NOVEL_CONFIG:-$WORKSPACE/novel-config.yml}"
VENV_PY="$WORKSPACE/dsh-vela/backend/.venv/bin/python3"
DASH_NOVEL="$WORKSPACE/dsh-vela/backend/.venv/bin/dsh-novel"
PORT="${DSH_NOVEL_PORT:-17861}"
LOG="$WORKSPACE/novel-server.log"

if [ ! -f "$CONFIG" ]; then
  echo "ERROR: config not found: $CONFIG" >&2
  exit 1
fi

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
