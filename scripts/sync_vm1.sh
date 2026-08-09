#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${VM1_SSH_HOST:-VM1}"
REMOTE_DIR="${VM1_REPO_DIR:-/home/orchestrator/multi-agent-orchestration}"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ssh "${REMOTE_HOST}" "mkdir -p '${REMOTE_DIR}'"

# Use tar over SSH so the remote VM does not need rsync installed.
tar -C "${LOCAL_DIR}" \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='.env' \
  --exclude='.local' \
  --exclude='data' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='.opencode/node_modules' \
  --exclude='.commandcode' \
  -cf - . \
  | ssh "${REMOTE_HOST}" "tar -xf - -C '${REMOTE_DIR}'"

echo "Synced source tree to ${REMOTE_HOST}:${REMOTE_DIR}"
echo "Runtime files preserved: .env, data/, .venv/, and .local/"
