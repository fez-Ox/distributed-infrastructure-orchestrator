# DESIGN.md

## 1. Overview
This document outlines the architectural design for an Option A centralized AI orchestration system hosted on Proxmox VE. The system uses a centralized "Hub-and-Spoke" topology where one AI-powered Orchestrator VM manages and configures many Proxmox guests, including LXCs and VMs.

The MVP intentionally starts with a single central AI agent rather than distributed child agents. Child nodes are initially managed infrastructure nodes that publish telemetry and accept configuration through Ansible over SSH. More nodes can be added over time by registering them in inventory and state.

Option B, distributed child-side agents, is an eventual required evolution after the centralized MVP is stable. The design should therefore avoid decisions that prevent later local agents on managed nodes.

To ensure reliability and separation of concerns, the system strictly separates the "Brain" (AI reasoning) from the "Hands" (deterministic execution). **NATS** is used as the high-performance message broker for telemetry and state synchronization, while **Ansible** is used as the idempotent execution engine for system configuration.

## 2. System Topology
The system consists of one Orchestrator VM and a variable number of managed nodes connected through the Proxmox network bridge (e.g., `vmbr0`) with stable IPs or resolvable hostnames.

*   **Orchestrator VM:** The central hub. For the current environment this is **VM1 (`192.168.122.88`)**. Hosts the NATS server, State Database, AI Orchestrator Agent, and Ansible control node.
*   **Managed Nodes:** LXCs or VMs managed by the orchestrator. Each node hosts a lightweight telemetry publisher and an SSH server for Ansible during the MVP. Later, each node will also host a local child agent for Option B distributed orchestration.
    *   Current cluster: **VM2** (`192.168.122.89`, worker VM), **LXC1** (`192.168.122.238`, worker), **LXC2** (`192.168.122.239`, worker).

Nodes are not hardcoded to a fixed set. The inventory and state database must support adding, removing, and disabling nodes over time.

## 3. Component Architecture

### 3.1 Communication Layer: NATS
NATS acts as the central nervous system for real-time telemetry and status events. It is deployed as a single server on the Orchestrator VM for the MVP.

**Subject Naming Convention:**
*   `telemetry.<node_id>.system`: Managed nodes publish CPU, RAM, disk, uptime, and host metadata every 5 seconds.
*   `state.<node_id>.status`: Managed nodes publish operational status such as `online`, `offline`, `configuring`, or `error`.
*   `events.<node_id>.*`: Optional node lifecycle or workload events.

NATS is not the primary command execution path. Configuration changes are applied through Ansible over SSH.

**Data Flow:**
1. Managed nodes push telemetry to `telemetry.<node_id>.system`.
2. The Orchestrator's State Manager subscribes to `telemetry.>` and upserts into the local database.
3. When the AI Agent needs cluster state, it queries the local database, *not* NATS directly, ensuring fast, synchronous reads for the LLM.
4. New nodes appear in the database through static registration, inventory sync, or first telemetry heartbeat.

**Telemetry Publisher** (`node_agent/telemetry_pub.py`):
- Uses raw BSD sockets (stdlib only) — no external dependencies on worker nodes.
- Publishes to NATS every 5 seconds with node_id, hostname, IP, node_type, role, cpu_percent, memory, disk, uptime.
- Only prints errors to stdout (no per-cycle success logging).

### 3.2 Execution Layer: Ansible
Ansible handles all state changes on managed nodes. It guarantees idempotency and prevents the AI from directly executing destructive, hallucinated bash commands.

There are two execution paths:

**Path A: Template Mode (default)**
1. The AI Agent converts the user request into a structured intent.
2. A policy validator checks the target node, requested action, and parameters.
3. A deterministic Jinja2 renderer converts the approved intent into a known-good Ansible playbook template.
4. The Ansible executor runs `--syntax-check` first, then transitions to `awaiting_approval`.
5. Interactive approval (y/N) presented to the user. On approval, the playbook executes.
6. Ansible connects via SSH, applies the configuration, and stores stdout/stderr in the task record.

**Path B: Freeform Mode** (toggled via `/freeform` in the REPL)
1. The AI Agent generates raw Ansible playbook YAML directly (no structured intent, no policy validation).
2. The playbook may target multiple hosts, use any `ansible.builtin.*` module, and contain multiple tasks.
3. The executor runs `--syntax-check`, then prompts for approval.
4. On approval, the playbook executes. Output is streamed live to the user.
5. This mode is explicitly for sandboxed experimentation — it bypasses template rendering and policy checks.

**Multi-Step Workflow Engine** (freeform mode only):
- The LLM decomposes complex requests into a plan of ordered steps.
- Each step is a self-contained Ansible playbook YAML.
- Steps execute sequentially. On any step failure, the plan halts.
- Each step's output is streamed live as it runs.
- Plans are stored in `plans` and `plan_steps` DB tables with per-step status tracking.

### 3.3 AI Orchestrator Agent (The "Brain")
The AI Agent is a **CLI REPL application** (`python3 -m orchestrator`) running on the Orchestrator VM. It wraps an LLM via LiteLLM and provides the user with interactive commands.

**REPL Commands:**
- `<prompt>` — Natural language request (template or freeform mode)
- `/nodes` — Show registered nodes with live telemetry
- `/topology` — Show cluster topology with groups (managed_nodes, lxcs, vms, orchestrator)
- `/history [n]` — Show last n tasks
- `/task <id>` — Show task details with Ansible output
- `/plan <id>` — Show plan details with per-step status
- `/approve <id>` — Approve and execute an awaiting task (fallback)
- `/reject <id>` — Reject an awaiting task (fallback)
- `/freeform` — Toggle freeform mode (prompt changes to `[freeform] orchestrator>`)
- `/help` — List commands
- `/exit` — Exit

**LLM Integration (Planner):**
- Uses **LiteLLM** with configurable provider (default: `gemini/gemma-4-31b-it`).
- API key from `.env` (`GEMINI_API_KEY` or `OPENAI_API_KEY`).
- Template mode: `response_format={"type": "json_object"}` for structured output.
- Freeform mode: `max_tokens=4096` for complex multi-step playbooks.
- Fallback intent/plan if the LLM is unreachable (single intent or single-step plan).

**LLM Context Injection:**
- `build_topology_context()` constructs a cluster topology string from DB nodes + telemetry.
- Includes: available groups (managed_nodes, lxcs, vms, orchestrator), per-node IPs, types, roles, CPU/RAM/disk/uptime.

### 3.4 State Database
The Orchestrator stores inventory, latest telemetry, and task history locally. SQLite is sufficient for the MVP.

Tables:
*   `nodes`: `node_id`, `hostname`, `ip`, `node_type`, `role`, `enabled`, `last_seen`.
*   `telemetry_latest`: latest CPU, RAM, disk, uptime, and timestamp per node.
*   `tasks`: user request, structured intent (JSON), target node, status, Ansible output, timestamps.
*   `plans`: user request, description, status (planning/awaiting_approval/executing/completed/failed).
*   `plan_steps`: per-step: description, playbook_yaml, verification_command, status, Ansible output.

Task statuses: `pending`, `planning`, `awaiting_approval`, `running`, `verifying`, `succeeded`, `failed`, `rejected`.

### 3.5 Action Catalog (Template Mode)
Five template actions with Jinja2 playbooks:

| Action | Params | Template |
|---|---|---|
| `install-pkg` | package, state (present/latest/absent) | `install-pkg.yml.j2` |
| `update-system` | (none) | `update-system.yml.j2` |
| `add-user` | username, groups, shell, ssh_key | `add-user.yml.j2` |
| `config-file` | path, content, mode | `config-file.yml.j2` |
| `restart-service` | service, state, enabled | `restart-service.yml.j2` |

Group targeting (managed_nodes, lxcs, vms, orchestrator) is supported for multi-node requests.

### 3.6 Eventual Option B: Distributed Child Agents
After the centralized orchestrator is working reliably, each managed node must gain a local child agent. These agents should not replace the central orchestrator immediately; they should be introduced gradually.

Eventual child-agent responsibilities:
*   Perform local health checks and richer workload introspection.
*   Report capabilities, installed services, and local constraints.
*   Execute only pre-approved local actions from a restricted action catalog.
*   Participate in task negotiation, such as reporting whether a node can accept a workload.
*   Continue publishing telemetry/status through NATS.

Option B guardrails:
*   The Orchestrator remains the source of policy, approvals, and global scheduling decisions.
*   Child agents must not accept arbitrary shell commands or arbitrary LLM-generated code.
*   Child agents must authenticate to NATS and identify themselves by registered `node_id`.
*   Ansible remains available as the bootstrap, recovery, and deterministic configuration path.

## 4. Security and Safety Guardrails
*   **SSH Key Authentication:** The Orchestrator VM uses SSH key-based authentication to access managed nodes. Password authentication is disabled where practical.
*   **Template Mode Guardrails:** The AI outputs structured intent only. A policy engine validates action types, params, and target nodes. Playbooks are rendered from known-good Jinja2 templates.
*   **Freeform Mode Guardrails:** Sandboxed mode — still requires syntax check and interactive approval before execution. The LLM is prompted to use `ansible.builtin.*` modules only, with no machine-enforceable check.
*   **Ansible Guardrails:** Avoid arbitrary `shell` or `command` modules in templates unless explicitly reviewed and approved.
*   **Human-in-the-Loop:** Both modes show the playbook/plan and require interactive y/N approval before execution. The `/approve` and `/reject` commands remain as fallbacks.
*   **Ansible Playbook Paths:** All playbooks are written to `data/playbooks/` as `task-{id}.yml` or `plan-{plan_id}-step-{idx}.yml`.
*   **Node Registration:** New nodes must be explicitly enabled in inventory/state before receiving changes.
