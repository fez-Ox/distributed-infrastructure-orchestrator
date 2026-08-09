# Multi-Agent Orchestration

MVP target: **VM1 is the Orchestrator**.

- VM1: `192.168.122.88`, runs NATS, SQLite state manager, Ansible control, and future AI planner.
- LXC1: `192.168.122.238`, managed Alpine node.
- LXC2: `192.168.122.239`, managed Alpine node.
- VM2: `192.168.122.89`, managed Debian VM worker.

## Run on VM1

Clone this repository on VM1, then run:

```bash
uv sync
make setup-vm1
make ansible-ping
```

Deploy telemetry publishers to all managed nodes:

```bash
TELEMETRY_NATS_URL=nats://192.168.122.88:4222 \
  ANSIBLE_CONFIG=ansible/ansible.cfg \
  uv run ansible-playbook -i ansible/inventory.ini ansible/playbooks/install_telemetry.yml
```

Deploy authenticated child agents and telemetry services:

```bash
ANSIBLE_CONFIG=ansible/ansible.cfg \
  uv run ansible-playbook -i ansible/inventory.ini \
  ansible/playbooks/install_child_agent.yml \
  -e @/path/to/ansible-vault-vars.json
```

Start/verify services:

```bash
make start-nats
make state-manager
make smoke-test
```

For a non-interactive VM1 supervisor, install the user service units (root is
required for `loginctl enable-linger` if they must survive logout):

```bash
bash scripts/install_vm1_user_services.sh
printf '/delegate lxc1 check disk and report local readiness\\ny\\n' \\
  >> data/orchestrator.input
```

The input reader starts at EOF, so service restarts do not replay old commands.

## Notes

- `managed_nodes` intentionally contains only worker nodes, not VM1.
- VM1 is listed under the `orchestrator` inventory group.
- Managed nodes publish telemetry to VM1 at `nats://192.168.122.88:4222`.
- To sync the current local working tree to VM1 without overwriting VM1 runtime state:

  ```bash
  make sync-vm1
  ```

  This preserves VM1's `.env`, `data/`, `.venv/`, and `.local/` directories.
- Orchestrator-side services connect to local NATS at `nats://127.0.0.1:4222`.
