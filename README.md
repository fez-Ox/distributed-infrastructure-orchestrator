# Multi-Agent Orchestration

MVP target: **VM1 is the Orchestrator**.

- VM1: `192.168.122.88`, runs NATS, SQLite state manager, Ansible control, and future AI planner.
- LXC1: `192.168.122.238`, managed Alpine node.
- LXC2: `192.168.122.239`, managed Alpine node.

## Run on VM1

Clone this repository on VM1, then run:

```bash
uv sync
make setup-vm1
make ansible-ping
```

Deploy telemetry publishers to the LXCs:

```bash
TELEMETRY_NATS_URL=nats://192.168.122.88:4222 \
  ANSIBLE_CONFIG=ansible/ansible.cfg \
  uv run ansible-playbook -i ansible/inventory.ini ansible/playbooks/install_telemetry.yml
```

Start/verify services:

```bash
make start-nats
make state-manager
make smoke-test
```

## Notes

- `managed_nodes` intentionally contains only worker nodes, not VM1.
- VM1 is listed under the `orchestrator` inventory group.
- Managed nodes publish telemetry to VM1 at `nats://192.168.122.88:4222`.
- Orchestrator-side services connect to local NATS at `nats://127.0.0.1:4222`.
