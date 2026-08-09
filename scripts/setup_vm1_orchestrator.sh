#!/usr/bin/env bash
set -euo pipefail

if command -v uv >/dev/null 2>&1; then
  uv sync
  uv run python -m orchestrator.db
else
  .venv/bin/python -m orchestrator.db
fi

if [[ ! -x .local/bin/nats-server ]]; then
  scripts/download_nats_server.sh
fi

set -a
[[ -f .env ]] && source .env
set +a
scripts/start_nats_local.sh
scripts/start_state_manager.sh

echo "VM1 orchestrator setup complete"
echo "NATS_URL=${NATS_URL:-nats://127.0.0.1:4222}"
echo "TELEMETRY_NATS_URL=${TELEMETRY_NATS_URL:-nats://192.168.122.88:4222}"
