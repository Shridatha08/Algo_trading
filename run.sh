#!/usr/bin/env bash
# Starts the Python market-data service and the Node dashboard together.
# Ctrl+C stops both.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_SERVICE_PORT="${PYTHON_SERVICE_PORT:-8100}"
NODE_PORT="${NODE_PORT:-3001}"
DATA_MODE="${DATA_MODE:-live}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PYTHON_SERVICE_URL="http://127.0.0.1:${PYTHON_SERVICE_PORT}"

PY_PID=""
NODE_PID=""

# npm spawns sh -> node --watch -> node, so the whole descendant tree must be signalled.
kill_tree() {
  local pid="$1" child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    kill_tree "$child"
  done
  kill -TERM "$pid" 2>/dev/null
}

cleanup() {
  trap - INT TERM EXIT
  echo ""
  echo "[run] shutting down..."
  for pid in "$NODE_PID" "$PY_PID"; do
    [[ -n "$pid" ]] && kill_tree "$pid"
  done
  sleep 1
  for pid in "$NODE_PID" "$PY_PID"; do
    [[ -n "$pid" ]] && kill -KILL "$pid" 2>/dev/null
  done
  wait 2>/dev/null
  echo "[run] stopped."
}
trap cleanup INT TERM EXIT

port_busy() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3<&- && return 0
  return 1
}

for port in "$PYTHON_SERVICE_PORT" "$NODE_PORT"; do
  if port_busy "$port"; then
    echo "[run] port $port is already in use. Stop that process or set PYTHON_SERVICE_PORT/NODE_PORT." >&2
    exit 1
  fi
done

if [[ ! -f "$ROOT_DIR/.env" ]]; then
  echo "[run] warning: .env not found; the service will start without AngelOne credentials." >&2
fi

if [[ ! -d "$ROOT_DIR/node-dashboard/node_modules" ]]; then
  echo "[run] installing dashboard dependencies..."
  (cd "$ROOT_DIR/node-dashboard" && npm install --silent) || exit 1
fi

echo "[run] python  : $PYTHON_SERVICE_URL (DATA_MODE=$DATA_MODE)"
echo "[run] dashboard: http://localhost:${NODE_PORT}"

cd "$ROOT_DIR/python-service"
# Process substitution keeps $! as the service PID; a pipeline would return sed's PID.
PYTHON_SERVICE_PORT="$PYTHON_SERVICE_PORT" DATA_MODE="$DATA_MODE" \
  "$PYTHON_BIN" -u -m app.main > >(sed -u 's/^/[py]   /') 2>&1 &
PY_PID=$!

STARTUP_TIMEOUT="${STARTUP_TIMEOUT:-240}"
attempts=$((STARTUP_TIMEOUT * 2))
waited=0

for _ in $(seq 1 "$attempts"); do
  port_busy "$PYTHON_SERVICE_PORT" && break
  kill -0 "$PY_PID" 2>/dev/null || { echo "[run] python service exited during startup." >&2; exit 1; }
  waited=$((waited + 1))
  if (( waited % 20 == 0 )); then
    echo "[run] still starting (AngelOne login + instrument master), $((waited / 2))s elapsed..."
  fi
  sleep 0.5
done

if ! port_busy "$PYTHON_SERVICE_PORT"; then
  echo "[run] python service did not become ready within ${STARTUP_TIMEOUT}s on port $PYTHON_SERVICE_PORT." >&2
  echo "[run] raise STARTUP_TIMEOUT if the scrip master download is slow." >&2
  exit 1
fi
echo "[run] python service is ready."

cd "$ROOT_DIR/node-dashboard"
PYTHON_SERVICE_URL="$PYTHON_SERVICE_URL" NODE_PORT="$NODE_PORT" \
  npm run dev > >(sed -u 's/^/[node] /') 2>&1 &
NODE_PID=$!

wait -n "$PY_PID" "$NODE_PID"
echo "[run] a service exited; stopping the other."
