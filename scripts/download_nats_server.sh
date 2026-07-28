#!/usr/bin/env bash
set -euo pipefail

VERSION="${NATS_SERVER_VERSION:-v2.14.3}"
ARCHIVE="nats-server-${VERSION}-linux-amd64.tar.gz"
URL="https://github.com/nats-io/nats-server/releases/download/${VERSION}/${ARCHIVE}"

mkdir -p .local/bin /tmp/mao-nats-server
curl -L --fail -o "/tmp/${ARCHIVE}" "${URL}"
tar -xzf "/tmp/${ARCHIVE}" -C /tmp/mao-nats-server
cp "/tmp/mao-nats-server/nats-server-${VERSION}-linux-amd64/nats-server" .local/bin/nats-server
chmod +x .local/bin/nats-server
.local/bin/nats-server --version
