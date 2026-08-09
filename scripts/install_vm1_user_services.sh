#!/usr/bin/env bash
set -euo pipefail

# Install persistent services without requiring root. For system-wide services,
# copy systemd/*.service to /etc/systemd/system as root instead.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
for unit in mao-nats.service mao-state-manager.service mao-orchestrator.service; do
  sed -e '/^User=/d' -e 's/multi-user.target/default.target/g' \
    "$ROOT_DIR/systemd/$unit" > "$UNIT_DIR/$unit"
done
systemctl --user daemon-reload
systemctl --user enable --now mao-nats.service mao-state-manager.service mao-orchestrator.service
systemctl --user --no-pager --full status mao-nats.service mao-state-manager.service mao-orchestrator.service
