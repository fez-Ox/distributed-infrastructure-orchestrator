from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_litellm import ChatLiteLLM
from langgraph.types import Command, interrupt

from orchestrator.db import connect
from orchestrator.executor import ansible_run, syntax_check
from orchestrator.models import ActionType, AgentState, Intent, NodeSelector, Plan, TaskStatus, TaskType, WorkflowStep
from orchestrator.policy import PolicyEngine
from orchestrator.playbook_validator import is_read_only_playbook, validate_playbook
from orchestrator.preflight import PreflightEngine, PreflightError
from orchestrator.prompts import SYSTEM_PROMPT, FREEFORM_WORKFLOW_PROMPT, CHILD_SYSTEM_PROMPT
from orchestrator.renderer import render_playbook
from orchestrator.snapshot import SnapshotManager, resolve_target

PLAYBOOK_DIR = Path(__file__).resolve().parent.parent / "data" / "playbooks"


def _log(message: str) -> None:
    print(message, flush=True)

_db: Optional[sqlite3.Connection] = None
_policy: Optional[PolicyEngine] = None
_preflight: Optional[PreflightEngine] = None
_skip_preflight: bool = False
_snapshot_mgr: Optional[SnapshotManager] = None
_template_llm: Optional[ChatLiteLLM] = None
_workflow_llm: Optional[ChatLiteLLM] = None
_child_llm: Optional[ChatLiteLLM] = None
_raw_llm: Optional[ChatLiteLLM] = None
_mode: str = "orchestrator"
_child_node_id: Optional[str] = None


def init_nodes(
    db: sqlite3.Connection,
    valid_node_ids: set[str],
    api_key: str,
    model: str = "gemini/gemma-4-31b-it",
    skip_preflight: bool = False,
    mode: str = "orchestrator",
    child_node_id: str | None = None,
) -> None:
    global _db, _policy, _preflight, _skip_preflight, _snapshot_mgr, _template_llm, _workflow_llm, _child_llm, _raw_llm, _mode, _child_node_id
    _db = db
    _mode = mode
    _child_node_id = child_node_id
    allowed_actions = None
    if mode == "child":
        from node_agent.child_catalog import CHILD_ACTIONS
        allowed_actions = CHILD_ACTIONS
    _policy = PolicyEngine(valid_node_ids=valid_node_ids, mode=mode, child_node_id=child_node_id, allowed_actions=allowed_actions)
    _preflight = PreflightEngine(db=db, mode=mode, child_node_id=child_node_id)
    _skip_preflight = skip_preflight
    _snapshot_mgr = SnapshotManager(db=db)

    _template_llm = None
    _workflow_llm = None
    _child_llm = None
    _raw_llm = None
    if api_key:
        _template_llm = ChatLiteLLM(
            model=model,
            temperature=0.1,
            max_tokens=1024,
            api_key=api_key,
        ).with_structured_output(Intent, method="json_mode")

        _workflow_llm = ChatLiteLLM(
            model=model,
            temperature=0.2,
            max_tokens=4096,
            api_key=api_key,
        ).with_structured_output(Plan, method="json_mode")

        _child_llm = ChatLiteLLM(
            model=model,
            temperature=0.1,
            max_tokens=1024,
            api_key=api_key,
        ).with_structured_output(Intent, method="json_mode")

        _raw_llm = ChatLiteLLM(
            model=model,
            temperature=0.2,
            max_tokens=2048,
            api_key=api_key,
        )


def _create_template_task(user_request: str, intent: Intent) -> int:
    assert _db is not None
    cursor = _db.execute(
        "INSERT INTO tasks (user_request, task_type, intent_json, target_node_id, status) VALUES (?, ?, ?, ?, ?)",
        (user_request, TaskType.template.value, intent.model_dump_json(), intent.target.node_id or intent.target.group or "", TaskStatus.pending.value),
    )
    task_id = cursor.lastrowid
    _db.execute(
        "INSERT INTO task_steps (task_id, step_index, description, playbook_yaml, status) VALUES (?, ?, ?, ?, ?)",
        (task_id, 0, intent.reason, "", TaskStatus.pending.value),
    )
    _db.commit()
    return task_id


def _transition(task_id: int, status: TaskStatus, error: Optional[str] = None) -> None:
    assert _db is not None
    now = datetime.now(timezone.utc).isoformat()
    _db.execute("UPDATE tasks SET status = ?, updated_at = ?, error = ? WHERE task_id = ?", (status.value, now, error, task_id))
    _db.commit()


def _update_step(task_id: int, step_index: int, **kwargs: Any) -> None:
    assert _db is not None
    now = datetime.now(timezone.utc).isoformat()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [now, task_id, step_index]
    _db.execute(f"UPDATE task_steps SET {sets}, updated_at = ? WHERE task_id = ? AND step_index = ?", vals)
    _db.commit()


def plan_node(state: AgentState) -> AgentState:
    user_input = state.get("user_input", "")
    topology = state.get("topology_context", "")
    _log(f"  → Planning: {user_input[:100]}")

    if _template_llm is None:
        intent = Intent(
            action=ActionType.update_system,
            target=NodeSelector(group="managed_nodes"),
            params={},
            reason="Fallback: LLM not configured",
        )
    else:
        try:
            intent: Intent = _template_llm.invoke([
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=f"{topology}\n\nUser request: {user_input}"),
            ])
        except Exception as e:
            intent = Intent(
                action=ActionType.update_system,
                target=NodeSelector(group="managed_nodes"),
                params={},
                reason=f"Fallback: LLM error: {e}",
            )

    task_id = _create_template_task(user_input, intent)
    _transition(task_id, TaskStatus.planning)
    _log(f"  ✓ Planned action '{intent.action.value}' for target '{intent.target.node_id or intent.target.group}' (task #{task_id})")

    state["intent"] = intent
    state["task_id"] = task_id
    state["error"] = None
    return state


def validate_node(state: AgentState) -> AgentState:
    assert _policy is not None
    assert _preflight is not None
    _log("  → Validating intent (policy + preflight)...")
    intent = state.get("intent")
    if intent is None:
        state["policy_ok"] = False
        state["error"] = "No intent to validate"
        _log("  ✗ Validation skipped: no intent")
        return state
    try:
        _policy.validate(intent)
        target_str = intent.target.node_id or intent.target.group or ""
        target_nodes = resolve_target(target_str)
        if not _skip_preflight:
            for nid in target_nodes:
                errors = _preflight.check_all(nid, intent.action)
                if errors:
                    raise PreflightError(f"Preflight check failed for '{nid}': {'; '.join(str(e) for e in errors)}")
        state["policy_ok"] = True
        state["task_target"] = target_str
        _log(f"  ✓ Intent valid, targets: {', '.join(target_nodes) or target_str}")
    except Exception as e:
        state["policy_ok"] = False
        state["error"] = str(e)
        _log(f"  ✗ Intent rejected: {e}")
        task_id = state.get("task_id")
        if task_id:
            _transition(task_id, TaskStatus.failed, error=str(e))
    return state


def route_after_validate(state: AgentState) -> str:
    if state.get("policy_ok"):
        return "render"
    return "__end__"


def render_node(state: AgentState) -> AgentState:
    intent = state.get("intent")
    task_id = state.get("task_id")
    if intent is None or task_id is None:
        state["error"] = "Missing intent or task_id for render"
        _log("  ✗ Render skipped: missing intent or task_id")
        return state
    _log("  → Rendering playbook...")
    try:
        if _mode == "child":
            from node_agent.child_catalog import render_child_playbook
            playbook_yaml = render_child_playbook(intent)
        else:
            playbook_yaml = render_playbook(intent)
        PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
        path = PLAYBOOK_DIR / f"task-{task_id}.yml"
        path.write_text(playbook_yaml, encoding="utf-8")
        state["playbook_path"] = str(path)
        _update_step(task_id, 0, playbook_yaml=playbook_yaml, playbook_path=str(path))
        _log(f"  ✓ Playbook rendered: {path}")
    except Exception as e:
        state["error"] = str(e)
        _transition(task_id, TaskStatus.failed, error=str(e))
        _log(f"  ✗ Render failed: {e}")
    return state


def syntax_check_node(state: AgentState) -> AgentState:
    playbook_path = state.get("playbook_path")
    task_id = state.get("task_id")
    if not playbook_path or task_id is None:
        state["syntax_ok"] = False
        state["error"] = "Missing playbook_path or task_id"
        _log("  ✗ Syntax check skipped: missing playbook_path or task_id")
        return state
    _log("  → Checking playbook syntax + guardrails...")
    try:
        ok, stdout, stderr = syntax_check(Path(playbook_path), local=(_mode == "child"))
        state["syntax_ok"] = ok
        state["ansible_stdout"] = stdout
        state["ansible_stderr"] = stderr
        if ok:
            assert _db is not None
            row = _db.execute(
                "SELECT playbook_yaml FROM task_steps WHERE task_id = ? AND step_index = 0", (task_id,)
            ).fetchone()
            if row and row[0]:
                val_errors = validate_playbook(row[0])
                if val_errors:
                    state["syntax_ok"] = False
                    state["error"] = "Playbook validation failed: " + "; ".join(val_errors)
                    _transition(task_id, TaskStatus.failed, error=state["error"])
                    _log(f"  ✗ Guardrail validation failed: {'; '.join(val_errors)}")
                    return state
            _transition(task_id, TaskStatus.awaiting_approval)
            _log("  ✓ Syntax and guardrails passed; awaiting approval")
        else:
            _transition(task_id, TaskStatus.failed, error="syntax check failed")
            _update_step(task_id, 0, ansible_stdout=stdout, ansible_stderr=stderr)
            _log("  ✗ Syntax check failed")
    except Exception as e:
        state["syntax_ok"] = False
        state["error"] = str(e)
        _log(f"  ✗ Syntax check errored: {e}")
    return state


def route_after_syntax(state: AgentState) -> str:
    if state.get("syntax_ok"):
        return "execute"
    return "__end__"


def execute_node(state: AgentState, config: RunnableConfig) -> AgentState:
    task_id = state.get("task_id")
    playbook_path = state.get("playbook_path")
    if task_id is None or not playbook_path:
        state["error"] = "Missing task_id or playbook_path"
        state["status"] = "failed"
        return state

    if _mode == "child":
        if not state.get("authorization_ok"):
            state["status"] = "rejected"
            _transition(task_id, TaskStatus.rejected)
            _log("  ✗ Task rejected: no valid authorization token")
            return state
    else:
        decision = interrupt("Approve this change?")
        if decision != "approve":
            state["status"] = "rejected"
            if task_id:
                _transition(task_id, TaskStatus.rejected)
            _log("  ✗ Task rejected by user")
            return state

    target_str = state.get("task_target", "")
    if _snapshot_mgr and _mode != "child":
        target_nodes = resolve_target(target_str)
        for nid in target_nodes:
            _snapshot_mgr.create(task_id=task_id, node_id=nid, step_id=0)

    _transition(task_id, TaskStatus.running)
    _update_step(task_id, 0, status=TaskStatus.running.value)
    _log(f"  → Running playbook via Ansible (task #{task_id})...")
    ok, stdout, stderr = ansible_run(Path(playbook_path), local=(_mode == "child"))
    _update_step(task_id, 0, ansible_stdout=stdout, ansible_stderr=stderr, status=TaskStatus.succeeded.value if ok else TaskStatus.failed.value)

    if ok:
        _transition(task_id, TaskStatus.verifying)
        _transition(task_id, TaskStatus.succeeded)
        state["status"] = "succeeded"
        _log(f"  ✓ Task #{task_id} succeeded")
    else:
        _transition(task_id, TaskStatus.failed)
        state["status"] = "failed"
        _log(f"  ✗ Task #{task_id} failed")

    state["ansible_stdout"] = stdout
    state["ansible_stderr"] = stderr
    return state


def _safe_read_only_audit_plan(user_input: str) -> Plan | None:
    text = user_input.lower()
    markers = ("read-only", "read only", "inspect", "audit", "health", "status", "readiness")
    if not any(marker in text for marker in markers):
        return None
    playbook = """---
- name: Read-only managed-node audit
  hosts: managed_nodes
  gather_facts: true
  tasks:
    - name: Collect service facts without changing services
      ansible.builtin.service_facts:
    - name: Calculate root filesystem readiness
      ansible.builtin.set_fact:
        root_free_bytes: "{{ (ansible_facts.mounts | selectattr('mount', 'equalto', '/') | map(attribute='size_available') | list | first | default(0)) | int }}"
        workload_ready: "{{ ((ansible_facts.mounts | selectattr('mount', 'equalto', '/') | map(attribute='size_available') | list | first | default(0)) | int) >= 1073741824 }}"
    - name: Report node audit
      ansible.builtin.debug:
        msg:
          node_id: "{{ inventory_hostname }}"
          os: "{{ ansible_facts.distribution | default('unknown') }} {{ ansible_facts.distribution_version | default('unknown') }}"
          init_system: "{{ ansible_facts.service_mgr | default('unknown') }}"
          package_manager: "{{ ansible_facts.pkg_mgr | default('unknown') }}"
          cpu_count: "{{ ansible_facts.processor_vcpus | default('unknown') }}"
          load: "{{ ansible_facts.loadavg | default({}) }}"
          memory_mb: "{{ ansible_facts.memtotal_mb | default('unknown') }}"
          root_free_bytes: "{{ root_free_bytes }}"
          workload_ready_for_1GiB: "{{ workload_ready }}"
          service_count: "{{ ansible_facts.services | default({}) | length }}"
- name: Verify VM1 policy authority without changing it
  hosts: localhost
  connection: local
  gather_facts: true
  tasks:
    - name: Report orchestrator identity
      ansible.builtin.debug:
        msg:
          policy_authority: "vm1"
          hostname: "{{ ansible_facts.hostname | default('unknown') }}"
"""
    return Plan(description=f"Safe fallback read-only audit: {user_input[:100]}", steps=[WorkflowStep(description="Inspect managed-node health and VM1 authority", playbook=playbook)])


def plan_workflow_node(state: AgentState) -> AgentState:
    assert _db is not None
    user_input = state.get("user_input", "")
    topology = state.get("topology_context", "")
    _log(f"  → Planning multi-step workflow: {user_input[:100]}")

    plan: Plan | None = None
    if _workflow_llm is not None:
        try:
            plan = _workflow_llm.invoke([
                SystemMessage(content=FREEFORM_WORKFLOW_PROMPT),
                HumanMessage(content=f"{topology}\n\nUser request: {user_input}"),
            ])
        except Exception as e:
            _log(f"  ! LLM workflow planning unavailable; using safe fallback: {e}")
    if plan is None:
        plan = _safe_read_only_audit_plan(user_input)
    if plan is None:
        _log("  ✗ No safe workflow could be planned; refusing to guess an infrastructure change")
        state["error"] = "LLM planning unavailable and request is not a recognized read-only audit"
        state["status"] = "rejected"
        return state

    cursor = _db.execute(
        "INSERT INTO tasks (user_request, task_type, plan_json, target_node_id, status) VALUES (?, ?, ?, ?, ?)",
        (user_input, TaskType.freeform.value, plan.model_dump_json(), "", "planning"),
    )
    task_id = cursor.lastrowid
    for i, step in enumerate(plan.steps):
        _db.execute(
            "INSERT INTO task_steps (task_id, step_index, description, playbook_yaml, rollback_playbook_yaml, status) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, i, step.description, step.playbook, step.rollback_playbook, "pending"),
        )
    _db.commit()

    state["plan"] = plan
    state["task_id"] = task_id
    state["current_step_index"] = 0
    _log(f"  ✓ Workflow planned with {len(plan.steps)} step(s) (task #{task_id})")
    return state


def route_after_workflow_plan(state: AgentState) -> str:
    if state.get("plan"):
        return "approve_plan"
    return "__end__"


def approve_plan_node(state: AgentState, config: RunnableConfig) -> AgentState:
    decision = interrupt("Approve this workflow?")
    if decision != "approve":
        if _db is not None and state.get("task_id"):
            _db.execute(
                "UPDATE task_steps SET status = 'skipped' WHERE task_id = ? AND status = 'pending'",
                (state["task_id"],),
            )
            _transition(state["task_id"], TaskStatus.rejected)
        state["status"] = "rejected"
        plan = state.get("plan")
        if plan:
            state["current_step_index"] = len(plan.steps)
        _log("  ✗ Workflow rejected")
        return state

    assert _db is not None
    task_id = state.get("task_id")
    if task_id is None:
        state["error"] = "Missing task_id"
        state["status"] = "failed"
        return state

    _transition(task_id, TaskStatus.running)
    state["current_step_index"] = 0
    _log("  ✓ Workflow approved; starting execution")
    return state


def route_after_step(state: AgentState) -> str:
    if state.get("status") in {"rejected", "failed"}:
        return "__end__"
    plan = state.get("plan")
    idx = state.get("current_step_index", 0)
    if plan and idx < len(plan.steps):
        return "execute_step"
    return "__end__"


def replan_failed_step(
    step_description: str,
    failed_playbook: str,
    error_output: str,
    topology_context: str,
) -> str | None:
    if _raw_llm is None:
        return None
    try:
        from orchestrator.prompts import REPLAN_STEP_PROMPT
        prompt_text = REPLAN_STEP_PROMPT.format(
            step_description=step_description,
            failed_playbook=failed_playbook,
            error_output=error_output,
            topology_context=topology_context,
        )
        response = _raw_llm.invoke([
            SystemMessage(content="You are an Ansible step debugger. Output ONLY valid YAML."),
            HumanMessage(content=prompt_text),
        ])
        content = response.content if hasattr(response, "content") else str(response)
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text += str(block.get("text", "")) + "\n"
                elif isinstance(block, str):
                    text += block + "\n"
        elif hasattr(response, "text") and isinstance(response.text, str):
            text = response.text

        text = text.strip()
        if "```yaml" in text:
            text = text.split("```yaml")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        if "hosts:" in text or "tasks:" in text or "---" in text:
            if not text.startswith("---"):
                text = "---\n" + text
            return text
    except Exception as e:
        _log(f"  ! Re-plan failed step error: {e}")
    return None


def execute_step_node(state: AgentState, config: RunnableConfig) -> AgentState:
    assert _db is not None
    assert _preflight is not None
    plan = state.get("plan")
    task_id = state.get("task_id")
    idx = state.get("current_step_index", 0)

    if plan is None or task_id is None or idx >= len(plan.steps):
        state["error"] = "No more steps to execute"
        state["status"] = "failed"
        return state

    step = plan.steps[idx]
    playbook_yaml = step.playbook
    _log(f"  → Executing step {idx + 1}/{len(plan.steps)}: {step.description[:80]}")

    validation_errors = validate_playbook(playbook_yaml, allow_command_modules=False)
    if validation_errors:
        state["error"] = "Playbook validation failed: " + "; ".join(validation_errors)
        state["status"] = "failed"
        _transition(task_id, TaskStatus.failed, error=state["error"])
        _update_step(task_id, idx, status=TaskStatus.failed.value, error=state["error"])
        _log(f"  ✗ Freeform playbook rejected: {'; '.join(validation_errors)}")
        return state

    playbook_path = PLAYBOOK_DIR / f"task-{task_id}-step-{idx}.yml"
    PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
    playbook_path.write_text(playbook_yaml, encoding="utf-8")
    syntax_ok, syntax_stdout, syntax_stderr = syntax_check(playbook_path)
    if not syntax_ok:
        state["error"] = syntax_stderr or "Ansible syntax check failed"
        state["ansible_stdout"] = syntax_stdout
        state["ansible_stderr"] = syntax_stderr
        state["status"] = "failed"
        _transition(task_id, TaskStatus.failed, error=state["error"])
        _update_step(task_id, idx, status=TaskStatus.failed.value, ansible_stdout=syntax_stdout, ansible_stderr=syntax_stderr, error=state["error"])
        _log("  ✗ Freeform playbook syntax check failed")
        return state

    from orchestrator.snapshot import parse_hosts_from_playbook as parse_hosts
    target_nodes = parse_hosts(playbook_yaml)
    if not _skip_preflight:
        for nid in target_nodes:
            errors = _preflight.check_all(nid, ActionType.freeform)
            if errors:
                state["error"] = "; ".join(str(e) for e in errors)
                state["status"] = "failed"
                _transition(task_id, TaskStatus.failed, error=state["error"])
                return state

    if _snapshot_mgr and not is_read_only_playbook(playbook_yaml):
        for nid in target_nodes:
            _snapshot_mgr.create(task_id=task_id, node_id=nid, step_id=idx)

    _update_step(task_id, idx, status=TaskStatus.running.value)

    ok, stdout, stderr = ansible_run(playbook_path)

    # Self-healing retry loop if initial Ansible execution fails
    retry_count = 0
    max_retries = 2
    topology = state.get("topology_context", "")

    while not ok and retry_count < max_retries:
        retry_count += 1
        _log(f"  → Initiating self-healing retry {retry_count}/{max_retries} for step {idx + 1}...")
        error_msg = stderr or stdout or "Unknown execution error"
        corrected_yaml = replan_failed_step(step.description, playbook_yaml, error_msg, topology)
        if not corrected_yaml:
            _log("  ! Self-healing could not generate a corrected playbook")
            break

        val_errors = validate_playbook(corrected_yaml, allow_command_modules=False)
        if val_errors:
            _log(f"  ! Corrected playbook failed validation: {'; '.join(val_errors)}")
            continue

        corr_path = PLAYBOOK_DIR / f"task-{task_id}-step-{idx}-retry-{retry_count}.yml"
        corr_path.write_text(corrected_yaml, encoding="utf-8")
        s_ok, s_stdout, s_stderr = syntax_check(corr_path)
        if not s_ok:
            _log("  ! Corrected playbook failed syntax check")
            continue

        _log(f"  ✓ Corrected playbook syntax passed; re-running step {idx + 1}...")
        ok, stdout, stderr = ansible_run(corr_path)
        if ok:
            _log(f"  ✓ Self-healing retry {retry_count} succeeded for step {idx + 1}!")
            playbook_yaml = corrected_yaml
            break

    state["ansible_stdout"] = stdout
    state["ansible_stderr"] = stderr
    state["current_step_index"] = idx + 1

    if ok:
        _update_step(task_id, idx, status=TaskStatus.succeeded.value, ansible_stdout=stdout, ansible_stderr=stderr)
        total = len(plan.steps)
        if idx + 1 >= total:
            _transition(task_id, TaskStatus.succeeded)
            state["status"] = "completed"
            _log(f"  ✓ Task #{task_id} completed ({total} step(s))")
        else:
            state["status"] = f"step_{idx + 1}_of_{total}_succeeded"
            _log(f"  ✓ Step {idx + 1}/{total} succeeded")
    else:
        state["error"] = stderr or "Ansible execution failed"
        _update_step(task_id, idx, status=TaskStatus.failed.value, ansible_stdout=stdout, ansible_stderr=stderr, error=stderr[:500] if stderr else None)
        _transition(task_id, TaskStatus.failed, error=stderr[:500] if stderr else None)
        state["status"] = "failed"
        _log(f"  ✗ Step {idx + 1}/{len(plan.steps)} failed")

    return state


def rollback_workflow_task(conn: sqlite3.Connection, task_id: int) -> tuple[bool, str]:
    """Execute reverse rollback playbooks for all completed steps in a workflow task."""
    steps = conn.execute(
        "SELECT step_index, description, playbook_yaml, rollback_playbook_yaml, status FROM task_steps WHERE task_id = ? AND status = 'succeeded' ORDER BY step_index DESC",
        (task_id,),
    ).fetchall()

    if not steps:
        return (False, f"Task #{task_id} has no succeeded steps to roll back")

    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?", (TaskStatus.rolling_back.value, now, task_id))
    conn.commit()
    _log(f"  → Initiating rollback for task #{task_id} ({len(steps)} step(s) to undo)")

    for step_row in steps:
        idx = step_row["step_index"]
        desc = step_row["description"]
        rb_yaml = step_row["rollback_playbook_yaml"]

        if not rb_yaml or not rb_yaml.strip():
            _log(f"  ! Step {idx + 1} has no rollback playbook; marking skipped")
            conn.execute(
                "UPDATE task_steps SET rollback_status = 'skipped', updated_at = ? WHERE task_id = ? AND step_index = ?",
                (datetime.now(timezone.utc).isoformat(), task_id, idx),
            )
            conn.commit()
            continue

        _log(f"  → Rolling back step {idx + 1}: {desc[:80]}")

        val_errors = validate_playbook(rb_yaml, allow_command_modules=False)
        if val_errors:
            err_msg = f"Rollback playbook validation failed: {'; '.join(val_errors)}"
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE task_steps SET rollback_status = 'failed', error = ?, updated_at = ? WHERE task_id = ? AND step_index = ?",
                (err_msg, now_iso, task_id, idx),
            )
            conn.execute("UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE task_id = ?", (TaskStatus.rollback_failed.value, err_msg, now_iso, task_id))
            conn.commit()
            _log(f"  ✗ Rollback step {idx + 1} validation failed: {err_msg}")
            return (False, err_msg)

        rb_path = PLAYBOOK_DIR / f"task-{task_id}-step-{idx}-rollback.yml"
        PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
        rb_path.write_text(rb_yaml, encoding="utf-8")

        syntax_ok, s_stdout, s_stderr = syntax_check(rb_path)
        if not syntax_ok:
            err_msg = s_stderr or "Rollback syntax check failed"
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE task_steps SET rollback_status = 'failed', rollback_ansible_stdout = ?, rollback_ansible_stderr = ?, error = ?, updated_at = ? WHERE task_id = ? AND step_index = ?",
                (s_stdout, s_stderr, err_msg, now_iso, task_id, idx),
            )
            conn.execute("UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE task_id = ?", (TaskStatus.rollback_failed.value, err_msg, now_iso, task_id))
            conn.commit()
            _log(f"  ✗ Rollback step {idx + 1} syntax check failed")
            return (False, err_msg)

        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE task_steps SET rollback_status = 'running', updated_at = ? WHERE task_id = ? AND step_index = ?",
            (now_iso, task_id, idx),
        )
        conn.commit()

        ok, stdout, stderr = ansible_run(rb_path)

        now_iso = datetime.now(timezone.utc).isoformat()
        if ok:
            conn.execute(
                "UPDATE task_steps SET rollback_status = 'succeeded', rollback_ansible_stdout = ?, rollback_ansible_stderr = ?, updated_at = ? WHERE task_id = ? AND step_index = ?",
                (stdout, stderr, now_iso, task_id, idx),
            )
            conn.commit()
            _log(f"  ✓ Rollback step {idx + 1} succeeded")
        else:
            err_msg = stderr or "Rollback execution failed"
            conn.execute(
                "UPDATE task_steps SET rollback_status = 'failed', rollback_ansible_stdout = ?, rollback_ansible_stderr = ?, error = ?, updated_at = ? WHERE task_id = ? AND step_index = ?",
                (stdout, stderr, err_msg, now_iso, task_id, idx),
            )
            conn.execute("UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE task_id = ?", (TaskStatus.rollback_failed.value, err_msg, now_iso, task_id))
            conn.commit()
            _log(f"  ✗ Rollback step {idx + 1} failed: {err_msg[:100]}")
            return (False, err_msg)

    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?", (TaskStatus.rolled_back.value, now_iso, task_id))
    conn.commit()
    _log(f"  ✓ Task #{task_id} fully rolled back")
    return (True, f"Task #{task_id} successfully rolled back")


###############################################################################
# Child agent planning (orchestrator side)
###############################################################################

_CHILD_ACTION_KEYWORDS: list[tuple[str, ActionType]] = [
    ("capabilit", ActionType.capabilities_report),
    ("readiness", ActionType.workload_readiness),
    ("capacity", ActionType.workload_readiness),
    ("disk", ActionType.disk_check),
    ("storage", ActionType.disk_check),
    ("free space", ActionType.disk_check),
    ("log", ActionType.log_tail),
    ("service", ActionType.service_status),
    ("status", ActionType.service_status),
    ("running", ActionType.service_status),
]

_STOPWORDS = {"the", "a", "an", "running", "stopped", "started", "online", "offline", "up", "down", "it", "on", "of"}


def _extract_service(text: str) -> str | None:
    for pattern in [
        r"\bstatus\s+of\s+([A-Za-z0-9_.-]+)",
        r"\bservice\s+([A-Za-z0-9_.-]+)",
        r"\bis\s+([A-Za-z0-9_.-]+)",
    ]:
        match = re.search(pattern, text)
        if match and match.group(1).lower() not in _STOPWORDS:
            return match.group(1)
    return None


def _fallback_child_intent(request: str, node_id: str) -> Intent | None:
    text = request.lower()
    action = None
    for keyword, candidate in _CHILD_ACTION_KEYWORDS:
        if keyword in text:
            action = candidate
            break
    if action is None:
        return None

    params: dict[str, Any] = {}
    if action == ActionType.service_status:
        service = _extract_service(text)
        if not service:
            return None
        params["service"] = service
    elif action == ActionType.log_tail:
        match = re.search(r"(/var/log/[A-Za-z0-9_./-]+)", request)
        params["path"] = match.group(1) if match else "/var/log/messages"
        params["lines"] = 20

    return Intent(action=action, target=NodeSelector(node_id=node_id), params=params, reason=request)


def plan_child_intent(request: str, node_id: str) -> Intent | None:
    if _child_llm is not None:
        try:
            intent = _child_llm.invoke([
                SystemMessage(content=CHILD_SYSTEM_PROMPT),
                HumanMessage(content=f"Node: {node_id}\nUser request: {request}"),
            ])
            intent.target.node_id = node_id
            intent.target.group = None
            return intent
        except Exception:
            pass
    return _fallback_child_intent(request, node_id)


def create_child_task(conn: sqlite3.Connection, node_id: str, request: str, intent: Intent) -> int:
    cursor = conn.execute(
        "INSERT INTO tasks (user_request, task_type, intent_json, target_node_id, status, execution_mode) VALUES (?, ?, ?, ?, ?, ?)",
        (request, TaskType.child.value, intent.model_dump_json(), node_id, TaskStatus.pending.value, "child"),
    )
    task_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO task_steps (task_id, step_index, description, playbook_yaml, status) VALUES (?, ?, ?, ?, ?)",
        (task_id, 0, intent.reason or request, "", TaskStatus.pending.value),
    )
    conn.commit()
    return task_id
