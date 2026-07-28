#!/usr/bin/env bash
set -euo pipefail

mkdir -p data
export NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"
export DATABASE_PATH="${DATABASE_PATH:-./data/cluster_state.db}"

if pgrep -af "orchestrator.state_manager" | grep -v grep >/dev/null; then
  echo "state manager already running"
  pgrep -af "orchestrator.state_manager" | grep -v grep
  exit 0
fi

setsid env NATS_URL="$NATS_URL" DATABASE_PATH="$DATABASE_PATH" \
  .venv/bin/python -m orchestrator.state_manager \
  > data/state-manager.log 2>&1 < /dev/null &

echo $! > data/state-manager.pid
sleep 1
pgrep -af "orchestrator.state_manager" | grep -v grep
