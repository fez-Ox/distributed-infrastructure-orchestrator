#!/usr/bin/env bash
set -euo pipefail

if [[ ! -x .local/bin/nats-server ]]; then
  echo "nats-server not found at .local/bin/nats-server; run scripts/download_nats_server.sh first" >&2
  exit 1
fi

mkdir -p data

if pgrep -x nats-server >/dev/null; then
  echo "nats-server already running"
  pgrep -af nats-server
  exit 0
fi

NATS_ARGS=(-a 0.0.0.0 -p 4222)
if [[ "${NATS_AUTH_REQUIRED:-false}" == "true" ]]; then
  if command -v uv >/dev/null 2>&1; then
    uv run python scripts/render_nats_config.py data/nats-server.conf
  else
    .venv/bin/python scripts/render_nats_config.py data/nats-server.conf
  fi
  NATS_ARGS=(-c data/nats-server.conf)
fi

nohup .local/bin/nats-server "${NATS_ARGS[@]}" > data/nats-server.log 2>&1 &
echo $! > data/nats-server.pid
sleep 1
pgrep -af nats-server
tail -20 data/nats-server.log
