#!/usr/bin/env bash
set -euo pipefail

ORCHESTRATOR_VM="${ORCHESTRATOR_VM:-vm1}"
WORKERS="${WORKERS:-vm2 lxc1 lxc2}"

echo "=== Stopping orchestration stack ==="

# 1. Kill telemetry_pub on all workers
for node in $WORKERS; do
    echo "  -> Killing telemetry_pub on $node"
    ssh "$node" "pkill -f telemetry_pub.py" 2>/dev/null || true
done

# 2. Kill telemetry_pub on orchestrator VM
echo "  -> Killing telemetry_pub on $ORCHESTRATOR_VM"
ssh "$ORCHESTRATOR_VM" "pkill -f telemetry_pub" 2>/dev/null || true

# 3. Kill state_manager on orchestrator VM
echo "  -> Killing state_manager on $ORCHESTRATOR_VM"
ssh "$ORCHESTRATOR_VM" "pkill -f state_manager" 2>/dev/null || true

# 4. Kill NATS server on orchestrator VM
echo "  -> Killing NATS server on $ORCHESTRATOR_VM"
ssh "$ORCHESTRATOR_VM" "pkill -f nats-server" 2>/dev/null || true

echo "=== Orchestration stack stopped ==="
