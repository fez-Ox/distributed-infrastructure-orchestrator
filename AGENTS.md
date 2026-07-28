# AGENTS.md

## Project Goal

Build a Proxmox-hosted orchestration system that starts with **Option A: centralized AI orchestration** and later evolves into **Option B: distributed child-side agents**.

MVP architecture:

- One Orchestrator VM.
- Many managed nodes: LXCs and/or VMs.
- NATS for telemetry, status, and events.
- SQLite for inventory, latest state, and task history.
- Ansible over SSH for deterministic configuration changes.
- A central LLM-powered planner that creates structured task intent.
- Deterministic code validates intent, renders approved Ansible templates, and executes tasks.

Long-term architecture:

- Managed nodes eventually run local child agents.
- Child agents report capabilities, health, workload status, and local constraints.
- Child agents may eventually perform restricted local actions.
- The Orchestrator remains the source of policy, approval, scheduling, and global state.

## Important Documents

Read these before making architectural or implementation changes:

- `DESIGN.md` — system architecture and safety model.
- `PLAN.md` — implementation phases and roadmap.
- `AGENTS.md` — instructions for coding agents working on this repo.

## Core Design Rules

1. Start with centralized orchestration.
2. Do not hardcode the system to only two LXCs.
3. Treat every managed machine as a registered node with a stable `node_id`.
4. Support both LXCs and VMs.
5. Use NATS for telemetry/status/events, not primary configuration execution.
6. Use Ansible for configuration changes.
7. Use SQLite for MVP state storage.
8. Keep the system extensible for future distributed child agents.

## AI Safety Rules

The LLM must not directly execute infrastructure changes.

For the MVP:

- The LLM may inspect cluster state through tools.
- The LLM may convert natural language into structured JSON intent.
- The LLM must choose from an allowed action catalog.
- The LLM must not generate arbitrary shell commands.
- The LLM must not generate arbitrary Ansible playbooks for execution.
- Deterministic code must validate every intent before execution.
- Deterministic code must render Ansible from known-good templates.
- Human approval is required before real changes are applied to managed nodes.

Preferred flow:

```text
User request
→ LLM structured intent
→ policy validation
→ deterministic playbook rendering
→ syntax/check mode
→ human approval
→ Ansible execution
→ telemetry/service verification
```

## Infrastructure Safety Rules

The coding agent may freely:

- Read and edit project files.
- Add tests.
- Run unit tests.
- Run linters/formatters if configured.
- Create scripts and configuration templates.
- Run local dry-run commands.

Ask for user approval before:

- Running real `ansible-playbook` against managed nodes.
- Installing packages on real nodes.
- Changing systemd services on real nodes.
- Rebooting or shutting down nodes.
- Deleting files or services on managed nodes.
- Making changes to the Proxmox host itself.
- Using credentials or modifying secret-bearing files.

Prefer dry-run/check mode first:

```bash
ansible-playbook --syntax-check ...
ansible-playbook --check ...
```

## Expected Repository Structure

Use this structure unless there is a good reason to change it:

```text
multi-agent-orchestration/
├── DESIGN.md
├── PLAN.md
├── AGENTS.md
├── README.md
├── pyproject.toml
├── .env.example
├── config/
│   ├── nodes.example.yaml
│   ├── ansible.cfg
│   └── inventory.ini
├── orchestrator/
│   ├── __init__.py
│   ├── main.py
│   ├── db.py
│   ├── state_manager.py
│   ├── policy.py
│   ├── renderer.py
│   ├── executor.py
│   └── agent.py
├── node_agent/
│   ├── __init__.py
│   ├── telemetry_pub.py
│   └── child_agent.py
├── ansible/
│   ├── inventory.ini
│   ├── playbooks/
│   └── templates/
├── scripts/
├── systemd/
├── tests/
└── data/
```

It is acceptable for early implementation to create only the directories/files needed for the current phase.

## Node Model

Do not assume only `lxc1` and `lxc2` exist. More nodes will be added.

Each node should have at least:

```yaml
node_id: lxc1
hostname: lxc1
ip: 10.0.0.11
node_type: lxc # or vm
ansible_user: root
enabled: true
role: worker
```

Node registration should support:

- adding nodes
- disabling nodes
- removing nodes
- distinguishing LXCs from VMs
- tracking telemetry freshness
- future child-agent status

## NATS Conventions

MVP subjects:

```text
telemetry.<node_id>.system
state.<node_id>.status
events.<node_id>.*
```

Future Option B subjects:

```text
agent.<node_id>.heartbeat
agent.<node_id>.capabilities
agent.<node_id>.status
agent.<node_id>.tasks.request
agent.<node_id>.tasks.result
```

Payloads should be JSON and include `node_id` and timestamp fields where relevant.

## SQLite State

MVP database should include at least:

- `nodes`
- `telemetry_latest`
- `tasks`

Task statuses should include:

```text
pending
planning
awaiting_approval
running
verifying
succeeded
failed
rejected
```

## Ansible Rules

Use Ansible as the deterministic execution layer.

Preferred modules:

- `apt`
- `dnf`/`yum` where needed
- `package`
- `systemd`
- `service`
- `copy`
- `template`
- `file`
- `user`
- `group`

Avoid arbitrary use of:

- `shell`
- `command`
- `raw`
- scripts copied and executed remotely

If these are genuinely required, they must be explicitly justified and require human approval.

## Implementation Order

Follow `PLAN.md`. In short:

1. Inventory, SSH, and Ansible.
2. NATS telemetry and SQLite state.
3. Deterministic orchestrator without AI.
4. Minimal AI planner.
5. End-to-end testing and node scaling.
6. Required evolution to distributed child agents.

Do not jump directly to distributed child agents before the centralized path is stable.

## Coding Style

- Prefer simple Python modules over complex frameworks early.
- Keep functions small and testable.
- Validate external inputs with explicit schemas where practical.
- Store config in files/environment variables, not hardcoded constants.
- Keep command execution isolated in one executor module.
- Log important task lifecycle events.
- Favor boring, reliable code over clever abstractions.

## Testing Expectations

When adding code, add or update tests where practical.

Useful test areas:

- intent validation
- policy checks
- playbook rendering
- database initialization/upserts
- task state transitions
- telemetry payload parsing

If a test framework is not yet configured, add a minimal `pytest` setup.

## Useful Commands

These commands may not exist yet. If adding project scaffolding, prefer making these available through a `Makefile` or scripts:

```bash
make test
make lint
make format
make ansible-ping
make smoke-test
```

Expected underlying commands may include:

```bash
python -m pytest
ansible managed_nodes -i ansible/inventory.ini -m ping
ansible-playbook --syntax-check -i ansible/inventory.ini <playbook.yml>
ansible-playbook --check -i ansible/inventory.ini <playbook.yml>
```

## Secrets and Environment

Do not commit real secrets.

Use `.env.example` for documented variables, such as:

```env
NATS_URL=nats://10.0.0.10:4222
DATABASE_PATH=./data/cluster_state.db
ANSIBLE_INVENTORY=./ansible/inventory.ini
LLM_PROVIDER=
OPENAI_API_KEY=
NODE_ID=
NODE_TYPE=
```

Real `.env` files should be gitignored once created.

## Final Reminder

This project should become a safe infrastructure orchestrator. The key principle is:

> The AI proposes. Deterministic code validates. Humans approve risky changes. Ansible executes. Telemetry verifies.
