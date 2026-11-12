# PLAN.md

## 1. Overview
This document provides a minimal implementation plan that starts with Option A: a centralized AI orchestrator managing many Proxmox LXCs/VMs. The MVP keeps one AI agent on the Orchestrator VM and treats all other machines as managed nodes. The design must support adding more nodes over time without code changes.

Option B, distributed child-side agents, is not part of the MVP but is a required later phase. The Option A implementation should be built in a way that allows child agents to be added without replacing the whole system.

**Current Status:** Phases 1–5 complete. Phase 6 (plan rollback) is next.
**Example Cluster:** `vm1` (orchestrator), `vm2` (worker), `lxc1` (worker), `lxc2` (worker).

## 2. Prerequisites
*   Proxmox VE environment with 1 Orchestrator VM and at least 2 managed nodes provisioned. In the example topology, `vm1` (`192.0.2.10`) is the Orchestrator and `lxc1`/`lxc2` are managed nodes. Managed nodes can be LXCs or VMs.
*   Stable IPs or DNS names assigned to all nodes. Example addresses use the RFC 5737 documentation range: `vm1` (192.0.2.10), `vm2` (192.0.2.11), `lxc1` (192.0.2.21), `lxc2` (192.0.2.22).
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
    vm1 ansible_host=192.0.2.10 ansible_user=orchestrator node_type=vm role=orchestrator

    [managed_nodes]
    lxc1 ansible_host=192.0.2.21 ansible_user=orchestrator node_type=lxc role=worker
    lxc2 ansible_host=192.0.2.22 ansible_user=orchestrator node_type=lxc role=worker
    vm2 ansible_host=192.0.2.11 ansible_user=orchestrator node_type=vm role=worker

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

## Phase 6: Plan Rollback ✅
**Goal:** Add ability to roll back partially-failed plans with paired reverse playbooks.

1.  **Extend WorkflowStep model:** Added `rollback_playbook` field — LLM generates paired reverse playbooks during planning.
2.  **Extend `task_steps` table:** Added `rollback_playbook_yaml`, `rollback_status`, `rollback_ansible_stdout`, and `rollback_ansible_stderr` columns.
3.  **Implement `nodes.rollback_workflow_task()`:** Executes reverse rollback playbooks in reverse order for completed steps with preflight reachability, policy gate validation, syntax checks, and immediate failure halting.
4.  **Add `/rollback <task_id>` REPL command:** Shows reverse rollback plan, asks for human approval `(y/N)`, executes with streaming output.
5.  **Interactive Step Failure Prompt:** Automatically prompts user on mid-workflow step failure to execute paired rollback steps.
6.  **Add `rolling_back`, `rolled_back`, `rollback_failed`** enum values to `TaskStatus`, `PlanStatus`, and `StepStatus`.

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

---

# Guardrail Implementation Phases

These phases implement defense-in-depth guardrails around the AI Orchestrator's
LLM component (see `plans/llm-guardrails.md` and `DESIGN.md`). Each phase is
independent and adds a containment layer — no single phase is the sole safety
mechanism.

## Phase 8: Pre-flight Precondition Checks ✅

**Goal:** Fail fast — verify node state and reachability before spending LLM
time or executing anything.

**Status:** Complete.

### Implementation

1. **New module `orchestrator/preflight.py`** with a `PreflightEngine` class:

   | Check | Method | Failure behavior |
   |---|---|---|
   | Node exists in DB and is enabled | `check_node_enabled(node_id)` | `PreflightError("Node x disabled")` |
   | Node registered in Ansible inventory | `check_inventory(node_id)` | `PreflightError("Node x not in inventory")` |
   | Node is SSH-reachable | `check_reachable(node_id)` | `PreflightError("Node x unreachable")` |
   | OS/distro compatibility | `check_os_compat(node_id, action_type)` | `PreflightError("install-pkg unsupported on Alpine")` |
   | Node has required capabilities | `check_capability(node_id, required)` | `PreflightError("Node x lacks systemd")` |

2. **Capability registry** — a static dict mapping action types to required node
   capabilities + approved OS families:
   ```python
   ACTION_CAPABILITIES = {
       "install-pkg": {"package_manager": ["apt", "apk", "dnf", "yum"]},
       "update-system": {"package_manager": ["apt", "apk", "dnf", "yum"]},
       "restart-service": {"init_system": ["systemd", "openrc"]},
       "add-user": {},   # universal
       "config-file": {}, # universal
   }
   ```

3. **Integration points:**
   - `validate_node()` in `nodes.py` — runs all preflight checks after policy
     validation, before rendering
   - `execute_step_node()` — runs node-level reachability check per step target

### Testing

```
tests/test_preflight.py
  - test_node_not_in_db — PreflightError raised
  - test_node_disabled — PreflightError raised
  - test_node_not_in_inventory — PreflightError raised
  - test_node_unreachable (mock ping fails) — PreflightError raised
  - test_os_mismatch — Debian action on Alpine node rejected
  - test_missing_capability — action requiring systemd on openrc-only node rejected
  - test_all_checks_pass — no PreflightError, engine returns ok
  - test_group_target_resolves_multi_node — all members checked
```

---

## Phase 9: Playbook Module Allowlist + Parameter Validation

**Goal:** Before any playbook runs, statically parse it and enforce that only
approved Ansible modules are used and all parameters pass bounds checking.

Status: Complete.

### Implementation

1. **New module `orchestrator/playbook_validator.py`**:

   - `module_allowlist(playbook_yaml)`: Parse YAML, extract all
     `ansible.builtin.*` module names, verify each is on the approved list.
   - `param_bounds_check(playbook_yaml)`: Validate parameter values against
     regex/schema patterns (package names, file paths, service names, etc.).

   **Default module allowlist (template mode):**

   ```
   ansible.builtin.package, ansible.builtin.apt, ansible.builtin.yum, ansible.builtin.dnf
   ansible.builtin.copy, ansible.builtin.template, ansible.builtin.file
   ansible.builtin.service, ansible.builtin.systemd
   ansible.builtin.user, ansible.builtin.group
   ansible.builtin.authorized_key
   ansible.builtin.archive, ansible.builtin.unarchive
   ansible.builtin.stat, ansible.builtin.debug, ansible.builtin.fail
   ansible.builtin.command.*, ansible.builtin.shell.*  # require override flag
   ```

   **Default parameter patterns:**
   | Field | Pattern | Rejects |
   |---|---|---|
   | `package` | `^[a-zA-Z0-9.+_-]+$` | `rm -rf /`, `.; cat /etc/shadow` |
   | `path` | `^/[a-zA-Z0-9_./-]+$` | `/etc/shadow`, `/`, `/root/.ssh` |
   | `content` | max length, no null bytes | binary/encoded payloads |
   | `service` | `^[a-zA-Z0-9_@.-]+$` | `../../etc/passwd`, `; rm` |
   | `username` | `^[a-z_][a-z0-9_-]*$` | `root`, `admin` (banned names) |

2. **Integration points:**
   - `syntax_check_node()` in `nodes.py` — runs after `--syntax-check`, before
     approval prompt
   - Freeform mode: runs module allowlist check (with stricter rules)
   - Results stored on `task_steps` as a new `validation_json` column (optional)

3. **Override mechanism:** A module-level or task-level flag
   `allow_disallowed_modules=True` that requires explicit human approval and logs
   the justification. Not available in template mode; only via freeform with
   `/override` command.

### Testing

```
tests/test_playbook_validator.py
  - Module allowlist:
    - test_allowed_module_passes — package module accepted
    - test_disallowed_module_fails — shell module rejected
    - test_override_flag_allows — override=True bypasses rejection
    - test_playbook_with_mixed_modules — allowed pass, disallowed fail
    - test_empty_playbook — rejected
    - test_malformed_yaml — rejected with parse error
  - Parameter bounds:
    - test_safe_package_name — passes
    - test_malicious_package_name — rejected (contains `;`)
    - test_absolute_path_allowed — /etc/nginx/nginx.conf passes
    - test_shadow_path_rejected — /etc/shadow rejected
    - test_null_bytes_in_content — rejected
    - test_banned_username — "root" as username rejected
    - test_valid_service_name — passes
    - test_invalid_service_name_with_slash — rejected
  - Combined:
    - test_full_validation_pipeline — both checks run in order
```

---

## Phase 10: Compositional Action DSL

**Goal:** Stop writing one Ansible template per workflow. Define composable,
individually-vetted primitives the LLM can sequence to satisfy arbitrary
requests without emitting raw YAML.

Status: Complete.

### Implementation

1. **Primitive catalog** in `orchestrator/primitives.py`:

   ```
   install_package(node_id, package, version=None)
   ensure_service_state(node_id, service, state: running|stopped|restarted)
   write_file(node_id, path, content, mode='0644')
   create_user(node_id, username, groups=None, shell=None, ssh_key=None)
   update_system(node_id)
   restart_service(node_id, service)
   ```

   Each primitive is a Pydantic model with:
   - `PrimitiveCall(id, params)` — validated parameters
   - `render()` → list of Ansible task dicts
   - `expected_effect()` → `EffectDescriptor` (services, files, packages touched)

2. **LLM output contract**: Instead of raw YAML or a single template selection,
   the LLM outputs a JSON object:
   ```json
   {
     "description": "Install nginx and enable it on all LXCs",
     "primitives": [
       {"action": "install_package", "params": {"package": "nginx"}},
       {"action": "ensure_service_state", "params": {"service": "nginx", "state": "running"}}
     ]
   }
   ```

3. **Renderer `render_primitives(primitives, target_nodes)`**:
   - Takes the primitive sequence and target nodes
   - Calls each primitive's `render()` to get task dicts
   - Assembles a single playbook YAML with `hosts: <group>` targeting
   - Passes through `--syntax-check`

4. **Effect descriptor**: Each primitive declares an `EffectDescriptor`:
   ```python
   @dataclass
   class EffectDescriptor:
       packages_installed: list[str] = field(default_factory=list)
       packages_removed: list[str] = field(default_factory=list)
       files_modified: list[str] = field(default_factory=list)
       services_affected: list[str] = field(default_factory=list)
       ports_opened: list[tuple[int, str]] = field(default_factory=list)
   ```
   The aggregate effect of a sequence is the union of all primitives' effects.

5. **Prompt update**: Template mode prompt changed to output primitive sequences
   instead of selecting from the 5 predefined actions. Freeform mode remains
   unchanged (it's an explicit escape hatch).

### Testing

```
tests/test_primitives.py
  - Each primitive:
    - test_install_package_renders_correct_yaml
    - test_ensure_service_state_renders_tasks
    - test_write_file_includes_content_and_mode
    - test_create_user_includes_ssh_key_when_given
    - test_update_system_renders_update_commands
    - test_restart_service_sets_enabled
  - Parameter validation:
    - test_invalid_package_name_rejected
    - test_invalid_service_state_rejected
    - test_missing_required_param_rejected
    - test_unknown_primitive_rejected
  - Effect descriptors:
    - test_install_package_effect_includes_package
    - test_write_file_effect_includes_path
    - test_aggregate_effect_is_union
    - test_empty_primitive_list_has_no_effect
  - Renderer:
    - test_single_primitive_renders_to_playbook
    - test_multi_primitive_playbook_has_all_tasks
    - test_group_target_resolves_correctly
    - test_syntax_check_passes_on_rendered_playbook
```

---

## Phase 11: Independent Verifier Pass

**Goal:** A second LLM call, with no shared reasoning trace from the first,
checks that the generated intent matches the original request's scope.

Status: Complete.

### Implementation

1. **New module `orchestrator/verifier.py`**:

   ```python
   class VerifierResult(enum.Enum):
       MATCH = "MATCH"
       SCOPE_MISMATCH = "SCOPE_MISMATCH"
       AMBIGUOUS = "AMBIGUOUS"

   def verify_intent(
       original_request: str,
       primitive_sequence: list[PrimitiveCall],
       topology_context: str,
   ) -> tuple[VerifierResult, str]:
   ```

2. **Verifier prompt**: Fresh context with only the original user request and the
   primitive sequence (no LLM reasoning trace). Instructed to output exactly one
   word + optional reason:

   ```
   You are a safety verifier. Given:
   - Original request: <user_request>
   - Proposed action sequence: <primitives>

   Output one of:
   MATCH
   SCOPE_MISMATCH <reason>
   AMBIGUOUS <reason>

   Check specifically:
   - Are all target node_ids implied by the request?
   - Does primitive count/type match the request's apparent complexity?
   - Any primitive touching nodes tagged role=critical without explicit mention?
   ```

3. **LLM call**: Uses the same LiteLLM model but a different system prompt.
   `temperature=0` for deterministic output. Fresh context — no conversation
   history from the planner.

4. **Integration point**: Called after primitive sequence is validated (Phase 10),
   before the approval prompt. `SCOPE_MISMATCH` or `AMBIGUOUS` halts the task
   and escalates to human review with the verifier's reason attached. The task
   is NOT retried silently — it's logged for audit.

5. **Audit log**: Every verifier result is stored on the task record:
   `verifier_result`, `verifier_reason`, `verifier_timestamp`.

### Testing

```
tests/test_verifier.py
  - test_match_on_simple_request — install htop → install_package(htop) → MATCH
  - test_scope_mismatch_extra_nodes — restart nginx → targets 10 nodes → SCOPE_MISMATCH
  - test_scope_mismatch_wrong_action — "check disk" → install_package → SCOPE_MISMATCH
  - test_ambiguous_vague_request — "fix the server" → AMBIGUOUS
  - test_verifier_logged_on_task — result stored in DB
  - test_verifier_blocks_execution_on_mismatch — task halted, not executed
  - test_verifier_does_not_retry_silently — second LLM call not made
  - test_no_shared_context — verifier prompt doesn't include planner reasoning
```

---

## Phase 12: Policy-as-Code Gate

**Goal:** A deterministic rules engine that runs on the compiled artifact,
enforcing hard invariants that no LLM output can override.

Status: Complete.

### Implementation

1. **New module `orchestrator/policy_gate.py`**:

   ```python
   @dataclass
   class PolicyVerdict:
       allowed: bool
       reason: str = ""
       escalate_to_human: bool = False

   class PolicyGate:
       def evaluate_playbook(
           playbook_yaml: str,
           target_nodes: list[str],
           task_metadata: dict,
       ) -> PolicyVerdict:
   ```

2. **Built-in rules** (implemented as composable predicate functions):

   | Rule | Logic | Output |
   |---|---|---|
   | `no_critical_nodes` | Deny if any target has `role=critical` without explicit human approval flag | `DENY` |
   | `module_allowlist` | Same as Phase 9, enforced here independently | `DENY` |
   | `blast_radius` | Reject if task touches > `MAX_NODES_PER_TASK` (default 10, configurable) | `DENY` |
   | `resource_bounds` | Reject if any package name or file path exceeds length limits or matches dangerous patterns | `DENY` |
   | `known_dangerous_patterns` | Reject playbooks containing `rm -rf /`, `> /dev/sda`, `chmod 777 /etc` | `DENY` |

3. **Verdict recording**: Every evaluated task gets `policy_verdict` and
   `policy_reason` stored on the task record in SQLite. If verdict is not
   `ALLOW`, the task is rejected before reaching the approval prompt.

4. **Escalation path**: `ESCALATE(reason)` — suspends the task and adds a
   `requires_human_review` flag. Human can override via `/force-approve <id>`
   (with justification logged).

### Testing

```
tests/test_policy_gate.py
  - Critical node rules:
    - test_critical_node_without_approval_denied
    - test_critical_node_with_approval_allowed
    - test_non_critical_node_passes
  - Blast radius:
    - test_single_node_task_passes
    - test_multi_node_task_under_limit_passes
    - test_task_exceeding_max_nodes_denied
  - Dangerous patterns:
    - test_rm_rf_in_playbook_denied
    - test_dev_sda_write_denied
    - test_chmod_777_etc_denied
  - Resource bounds:
    - test_excessively_long_package_name_denied
    - test_null_bytes_in_playbook_denied
  - Combined:
    - test_all_rules_evaluated_independently
    - test_first_deny_stops_evaluation
    - test_verdict_stored_on_task_record
    - test_escalation_requires_human_review
```
