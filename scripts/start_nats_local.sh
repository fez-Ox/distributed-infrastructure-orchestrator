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

nohup .local/bin/nats-server -a 0.0.0.0 -p 4222 > data/nats-server.log 2>&1 &
echo $! > data/nats-server.pid
sleep 1
pgrep -af nats-server
tail -20 data/nats-server.log
