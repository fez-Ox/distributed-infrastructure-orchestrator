#!/usr/bin/env bash
set -euo pipefail

uv sync
uv run python -m orchestrator.db

if [[ ! -x .local/bin/nats-server ]]; then
  scripts/download_nats_server.sh
fi

scripts/start_nats_local.sh
scripts/start_state_manager.sh

echo "VM1 orchestrator setup complete"
echo "NATS_URL=${NATS_URL:-nats://127.0.0.1:4222}"
echo "TELEMETRY_NATS_URL=${TELEMETRY_NATS_URL:-nats://192.168.122.88:4222}"
