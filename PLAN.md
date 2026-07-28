# PLAN.md

## 1. Overview
This document provides a minimal implementation plan that starts with Option A: a centralized AI orchestrator managing many Proxmox LXCs/VMs. The MVP keeps one AI agent on the Orchestrator VM and treats all other machines as managed nodes. The design must support adding more nodes over time without code changes.

Option B, distributed child-side agents, is not part of the MVP but is a required later phase. The Option A implementation should be built in a way that allows child agents to be added without replacing the whole system.

**Current Status:** Phases 1–5 complete. Phase 6 (plan rollback) is next.
**Current Cluster:** VM1 (orchestrator), VM2 (worker), LXC1 (worker), LXC2 (worker).

## 2. Prerequisites
*   Proxmox VE environment with 1 Orchestrator VM and at least 2 managed nodes provisioned. In the current environment, **VM1 (`192.168.122.88`) is the Orchestrator** and LXC1/LXC2 are managed nodes. Managed nodes can be LXCs or VMs.
*   Stable IPs or DNS names assigned to all nodes: VM1 (192.168.122.88), VM2 (192.168.122.89), LXC1 (192.168.122.238), LXC2 (192.168.122.239).
*   SSH access configured from the Orchestrator to each managed node.
*   Python 3.10+ installed on all nodes.
*   A node naming convention, e.g. `lxc1`, `lxc2`, `vm1`, `worker-001`.

---

## Phase 1: Inventory, SSH, and Ansible ✅
**Goal:** Prove the Orchestrator can manage any registered LXC/VM over SSH.

1.  **Configure SSH Keys:** Keypair generated on Orchestrator, public key copied to all managed nodes.
2.  **Install and Configure Ansible:** Ansible installed in `.venv` on VM1. Inventory file at `ansible/inventory.ini` with groups:
    ```ini
    [orchestrator]
    vm1 ansible_host=192.168.122.88 ansible_user=orchestrator node_type=vm role=orchestrator

    [managed_nodes]
    lxc1 ansible_host=192.168.122.238 ansible_user=orchestrator node_type=lxc role=worker
    lxc2 ansible_host=192.168.122.239 ansible_user=orchestrator node_type=lxc role=worker
    vm2 ansible_host=192.168.122.89 ansible_user=orchestrator node_type=vm role=worker

    [lxcs]
    lxc1
    lxc2

    [vms]
    vm1
    vm2
    ```
3.  **Test Basic Execution:** `ansible managed_nodes -m ping` verified.
4.  **Node Add Process:** Add node to inventory.ini, copy SSH key, run `ansible <node> -m ping`.

## Phase 2: NATS Telemetry and State Database ✅
**Goal:** Establish real-time telemetry from every managed node to the Orchestrator.

1.  **Deploy NATS on Orchestrator:** NATS server downloaded and runs on VM1 (`nats-server -p 4222`). No systemd service — started manually via nohup.
2.  **Develop Telemetry Publisher:** `node_agent/telemetry_pub.py` uses raw BSD sockets (stdlib only) — no `nats-py` on workers. Publishes CPU/RAM/disk/uptime every 5s to `telemetry.<node_id>.system`.
3.  **Develop State Manager:** `orchestrator/state_manager.py` subscribes to `telemetry.>` via `nats-py`, upserts into SQLite (`data/cluster_state.db`). Only prints errors.
4.  **Deployment:** Deployed to VM1 (orchestrator role), VM2, LXC1, LXC2. All 4 nodes confirmed publishing telemetry with correct roles.

## Phase 3: Deterministic Orchestrator Without AI ✅
**Goal:** Build the safe execution pipeline before connecting an LLM.

1.  **Create Task Model:** Tasks stored in SQLite with statuses: `pending`, `planning`, `awaiting_approval`, `running`, `verifying`, `succeeded`, `failed`, `rejected`.
2.  **Create Allowed Action Catalog:** 5 actions with Pydantic param models:
    *   `install-pkg`: package, state (present/latest/absent)
    *   `update-system`: (no params)
    *   `add-user`: username, groups, shell, ssh_key
    *   `config-file`: path, content, mode
    *   `restart-service`: service, state, enabled
3.  **Create Playbook Templates:** 5 Jinja2 templates in `ansible/templates/*.j2`. Render via `orchestrator/renderer.py` using `StrictUndefined`.
4.  **Create Executor:** `orchestrator/executor.py` with:
    *   `validate()` — target node exists, action params valid, state transitions allowed (PolicyEngine)
    *   `--syntax-check` before approval
    *   Interactive y/N approval prompt
    *   `approve_and_run()` — executes playbook, stores stdout/stderr
    *   `.venv/bin` automatically added to PATH for subprocess

## Phase 4: Minimal AI Planner ✅
**Goal:** Add AI only for natural-language-to-intent planning.

1.  **Set up AI Framework:**
    *   **LiteLLM** with `gemini/gemma-4-31b-it` (configurable via `LLM_MODEL` env var).
    *   API key from `.env` (`GEMINI_API_KEY`).
    *   Dependencies installed via `uv sync` in `.venv`.
2.  **Define Planner:**
    *   Template mode (`plan()`): `response_format=json_object`, `max_tokens=1024`.
    *   Freeform mode (`plan_freeform()`): raw YAML output, `max_tokens=2048`.
    *   Workflow mode (`plan_workflow()`): multi-step plan, `max_tokens=4096`.
    *   Fallback intent/plan if LLM is unreachable.
3.  **System Prompts:**
    *   Template: structured JSON intent from allowed action catalog, group-based multi-node targeting.
    *   Freeform: raw Ansible playbook YAML, any `ansible.builtin.*` module.
    *   Workflow: multi-step plan with descriptions, playbooks, and optional verification commands.
4.  **Topology Context:** `build_topology_context()` injects node list with groups (managed_nodes, lxcs, vms, orchestrator), per-node telemetry.

## Phase 5: Interactive CLI REPL + Two Execution Modes ✅
**Goal:** Full end-to-end flow with template mode (safe) and freeform mode (flexible).

1.  **CLI REPL** (`orchestrator/agent.py`):
    *   Commands: `/nodes`, `/topology`, `/history`, `/task`, `/plan`, `/approve`, `/reject`, `/freeform`, `/help`, `/exit`
    *   Dotenv loading at startup
    *   Node seeding from `ansible/inventory.ini`
2.  **Interactive Approval:** After plan/playbook creation, user is prompted `Approve this plan? (y/N):`. `y` executes immediately; anything else rejects.
3.  **Freeform Mode** (`/freeform` toggle):
    *   Prompt changes to `[freeform] orchestrator>`
    *   Bypasses policy validation and template rendering
    *   Still requires syntax check + approval
4.  **Multi-Step Workflow Engine** (freeform mode only):
    *   Planner decomposes requests into ordered steps with descriptions
    *   `WorkflowExecutor` executes steps sequentially with streaming output
    *   Plans stored in `plans` + `plan_steps` tables
    *   On step failure: plan halts, remaining steps skipped
5.  **Streaming Output:** `execute_plan()` is a Python generator — Ansible output displayed line-by-line in real-time.
6.  **46 unit tests passing** across all components.

## Phase 6: Plan Rollback
**Goal:** Add ability to roll back partially-failed plans. (Upcoming)

1.  **Extend WorkflowStep model:** Add `rollback_playbook_yaml` field — LLM generates rollback playbooks during planning.
2.  **Extend `plan_steps` table:** Add `rollback_playbook_yaml` column.
3.  **Implement `WorkflowExecutor.rollback_plan()`:** Execute rollback playbooks in reverse order for completed steps.
4.  **Add `/rollback <plan_id>` REPL command:** Show rollback plan, ask for approval, execute with streaming.
5.  **Add `PlanStatus.rolled_back`** enum value.

## Phase 7: Required Evolution to Option B Distributed Agents
**Goal:** Add local child agents to managed nodes after the centralized system is stable.

1.  **Define Child Agent Contract:**
    *   Each child agent has a registered `node_id`.
    *   Each child agent authenticates to NATS.
    *   Each child agent publishes capabilities, health, workload status, and local constraints.
    *   Each child agent accepts only restricted, schema-validated actions.
2.  **Extend NATS Subjects:**
    *   `agent.<node_id>.heartbeat`
    *   `agent.<node_id>.capabilities`
    *   `agent.<node_id>.status`
    *   `agent.<node_id>.tasks.request`
    *   `agent.<node_id>.tasks.result`
3.  **Implement Child Agent MVP:**
    *   Start with read-only behavior: telemetry, capability reporting, and service introspection.
    *   Deploy it to nodes using Ansible.
    *   Store agent status in SQLite.
4.  **Add Restricted Local Actions:**
    *   Allow only small approved actions, e.g. service status checks, log summaries, workload readiness checks.
    *   Keep configuration-changing actions in Ansible until the child-agent trust model is proven.
5.  **Add Task Negotiation:**
    *   Orchestrator asks candidate nodes whether they can accept a workload.
    *   Child agents respond with local facts and constraints.
    *   Orchestrator remains the final scheduler and policy authority.
6.  **Gradually Expand Autonomy:**
    *   Add safe local remediation actions only after approval/policy controls exist.
    *   Keep Ansible as bootstrap, recovery, and deterministic fallback.

## 3. Future Expansions (Post-MVP)
*   **Dynamic Inventory:** Replace static `inventory.ini` with a custom Ansible dynamic inventory script backed by SQLite.
*   **Proxmox API Integration:** Discover LXCs/VMs directly from Proxmox and register them automatically as disabled until approved.
*   **Child-to-Child Clustering:** Allow templates and future child agents to coordinate replication/load balancing setups.
*   **NATS JetStream:** Upgrade from core NATS to JetStream for persistent telemetry history and event sourcing.
*   **Ansible Fact Gathering:** Feed `setup` module output into the LLM context for OS-aware planning.
*   **Workflow Verification Hooks:** Post-step verification commands (already schema-supported, not yet executed).
*   **Distributed Agents:** Required post-MVP evolution through Phase 7, not optional.
