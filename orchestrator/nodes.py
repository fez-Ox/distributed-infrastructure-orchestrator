from __future__ import annotations

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
from orchestrator.models import ActionType, AgentState, Intent, NodeSelector, Plan, TaskStatus, WorkflowStep
from orchestrator.policy import PolicyEngine
from orchestrator.prompts import SYSTEM_PROMPT, FREEFORM_WORKFLOW_PROMPT
from orchestrator.renderer import render_playbook
from orchestrator.snapshot import SnapshotManager, resolve_target

PLAYBOOK_DIR = Path(__file__).resolve().parent.parent / "data" / "playbooks"

_db: Optional[sqlite3.Connection] = None
_policy: Optional[PolicyEngine] = None
_snapshot_mgr: Optional[SnapshotManager] = None
_template_llm: Optional[ChatLiteLLM] = None
_workflow_llm: Optional[ChatLiteLLM] = None


def init_nodes(
    db: sqlite3.Connection,
    valid_node_ids: set[str],
    api_key: str,
    model: str = "gemini/gemma-4-31b-it",
) -> None:
    global _db, _policy, _snapshot_mgr, _template_llm, _workflow_llm
    _db = db
    _policy = PolicyEngine(valid_node_ids=valid_node_ids)
    _snapshot_mgr = SnapshotManager(db=db)

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


def _create_task(user_request: str, intent: Intent) -> int:
    assert _db is not None
    cursor = _db.execute(
        "INSERT INTO tasks (user_request, intent_json, target_node_id, status) VALUES (?, ?, ?, ?)",
        (user_request, intent.model_dump_json(), intent.target.node_id or intent.target.group or "", TaskStatus.pending.value),
    )
    _db.commit()
    return cursor.lastrowid


def _transition(task_id: int, status: TaskStatus, error: Optional[str] = None) -> None:
    assert _db is not None
    now = datetime.now(timezone.utc).isoformat()
    _db.execute("UPDATE tasks SET status = ?, updated_at = ?, error = ? WHERE task_id = ?", (status.value, now, error, task_id))
    _db.commit()


def _set_ansible_output(task_id: int, stdout: str, stderr: str) -> None:
    assert _db is not None
    _db.execute("UPDATE tasks SET ansible_stdout = ?, ansible_stderr = ?, updated_at = ? WHERE task_id = ?", (stdout, stderr, datetime.now(timezone.utc).isoformat(), task_id))
    _db.commit()


def plan_node(state: AgentState) -> AgentState:
    user_input = state.get("user_input", "")
    topology = state.get("topology_context", "")

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

    task_id = _create_task(user_input, intent)
    _transition(task_id, TaskStatus.planning)

    state["intent"] = intent
    state["task_id"] = task_id
    state["error"] = None
    return state


def validate_node(state: AgentState) -> AgentState:
    assert _policy is not None
    intent = state.get("intent")
    if intent is None:
        state["policy_ok"] = False
        state["error"] = "No intent to validate"
        return state
    try:
        _policy.validate(intent)
        state["policy_ok"] = True
        state["task_target"] = intent.target.node_id or intent.target.group or ""
    except Exception as e:
        state["policy_ok"] = False
        state["error"] = str(e)
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
        return state
    try:
        playbook_yaml = render_playbook(intent)
        PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
        path = PLAYBOOK_DIR / f"task-{task_id}.yml"
        path.write_text(playbook_yaml, encoding="utf-8")
        state["playbook_path"] = str(path)
    except Exception as e:
        state["error"] = str(e)
        _transition(task_id, TaskStatus.failed, error=str(e))
    return state


def syntax_check_node(state: AgentState) -> AgentState:
    playbook_path = state.get("playbook_path")
    task_id = state.get("task_id")
    if not playbook_path or task_id is None:
        state["syntax_ok"] = False
        state["error"] = "Missing playbook_path or task_id"
        return state
    try:
        ok, stdout, stderr = syntax_check(Path(playbook_path))
        state["syntax_ok"] = ok
        state["ansible_stdout"] = stdout
        state["ansible_stderr"] = stderr
        if ok:
            _transition(task_id, TaskStatus.awaiting_approval)
        else:
            _transition(task_id, TaskStatus.failed, error="syntax check failed")
            _set_ansible_output(task_id, stdout, stderr)
    except Exception as e:
        state["syntax_ok"] = False
        state["error"] = str(e)
    return state


def route_after_syntax(state: AgentState) -> str:
    if state.get("syntax_ok"):
        return "execute"
    return "__end__"


def execute_node(state: AgentState, config: RunnableConfig) -> AgentState:
    decision = interrupt("Approve this change?")
    if decision != "approve":
        state["status"] = "rejected"
        task_id = state.get("task_id")
        if task_id:
            _transition(task_id, TaskStatus.rejected)
        return state

    task_id = state.get("task_id")
    playbook_path = state.get("playbook_path")
    if task_id is None or not playbook_path:
        state["error"] = "Missing task_id or playbook_path"
        state["status"] = "failed"
        return state

    target_str = state.get("task_target", "")
    if _snapshot_mgr:
        target_nodes = resolve_target(target_str)
        for nid in target_nodes:
            _snapshot_mgr.create("task", task_id, nid)

    _transition(task_id, TaskStatus.running)
    ok, stdout, stderr = ansible_run(Path(playbook_path))
    _set_ansible_output(task_id, stdout, stderr)

    if ok:
        _transition(task_id, TaskStatus.verifying)
        _transition(task_id, TaskStatus.succeeded)
        state["status"] = "succeeded"
    else:
        _transition(task_id, TaskStatus.failed)
        state["status"] = "failed"

    state["ansible_stdout"] = stdout
    state["ansible_stderr"] = stderr
    return state


def plan_workflow_node(state: AgentState) -> AgentState:
    assert _db is not None
    user_input = state.get("user_input", "")
    topology = state.get("topology_context", "")

    if _workflow_llm is None:
        plan = Plan(
            description=f"Fallback: {user_input[:100]}",
            steps=[
                WorkflowStep(
                    description="Update all packages",
                    playbook="---\n- hosts: managed_nodes\n  gather_facts: no\n  tasks:\n    - name: Update all packages\n      ansible.builtin.package:\n        name: '*'\n        state: latest\n",
                )
            ],
        )
    else:
        try:
            plan: Plan = _workflow_llm.invoke([
                SystemMessage(content=FREEFORM_WORKFLOW_PROMPT),
                HumanMessage(content=f"{topology}\n\nUser request: {user_input}"),
            ])
        except Exception as e:
            plan = Plan(
                description=f"Fallback: {user_input[:100]}",
                steps=[
                    WorkflowStep(
                        description="Update all packages",
                        playbook="---\n- hosts: managed_nodes\n  gather_facts: no\n  tasks:\n    - name: Update all packages\n      ansible.builtin.package:\n        name: '*'\n        state: latest\n",
                    )
                ],
        )

    cursor = _db.execute(
        "INSERT INTO plans (description, user_request, status) VALUES (?, ?, ?)",
        (plan.description, user_input, "planning"),
    )
    plan_id = cursor.lastrowid
    for i, step in enumerate(plan.steps):
        _db.execute(
            "INSERT INTO plan_steps (plan_id, step_index, description, playbook_yaml, verification_command, status) VALUES (?, ?, ?, ?, ?, ?)",
            (plan_id, i, step.description, step.playbook, step.verification, "pending"),
        )
    _db.commit()

    state["plan"] = plan
    state["plan_id"] = plan_id
    state["current_step_index"] = 0
    return state


def route_after_workflow_plan(state: AgentState) -> str:
    if state.get("plan"):
        return "approve_plan"
    return "__end__"


def approve_plan_node(state: AgentState, config: RunnableConfig) -> AgentState:
    decision = interrupt("Approve this workflow?")
    if decision != "approve":
        if _db is not None and state.get("plan_id"):
            _db.execute(
                "UPDATE plan_steps SET status = 'skipped' WHERE plan_id = ? AND status = 'pending'",
                (state["plan_id"],),
            )
            _db.execute("UPDATE plans SET status = 'failed' WHERE plan_id = ?", (state["plan_id"],))
            _db.commit()
        state["status"] = "rejected"
        return state

    assert _db is not None
    plan_id = state.get("plan_id")
    if plan_id is None:
        state["error"] = "Missing plan_id"
        state["status"] = "failed"
        return state

    _db.execute("UPDATE plans SET status = 'executing' WHERE plan_id = ?", (plan_id,))
    _db.commit()
    state["current_step_index"] = 0
    return state


def route_after_step(state: AgentState) -> str:
    plan = state.get("plan")
    idx = state.get("current_step_index", 0)
    if plan and idx < len(plan.steps):
        return "execute_step"
    return "__end__"


def execute_step_node(state: AgentState, config: RunnableConfig) -> AgentState:
    assert _db is not None
    plan = state.get("plan")
    plan_id = state.get("plan_id")
    idx = state.get("current_step_index", 0)

    if plan is None or plan_id is None or idx >= len(plan.steps):
        state["error"] = "No more steps to execute"
        state["status"] = "failed"
        return state

    step = plan.steps[idx]
    playbook_yaml = step.playbook

    from orchestrator.snapshot import parse_hosts_from_playbook as parse_hosts
    target_nodes = parse_hosts(playbook_yaml)
    if _snapshot_mgr:
        for nid in target_nodes:
            _snapshot_mgr.create("plan_step", (plan_id << 16) | idx, nid)

    _db.execute(
        "UPDATE plan_steps SET status = 'running' WHERE plan_id = ? AND step_index = ?",
        (plan_id, idx),
    )
    _db.commit()

    playbook_path = PLAYBOOK_DIR / f"plan-{plan_id}-step-{idx}.yml"
    PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
    playbook_path.write_text(playbook_yaml, encoding="utf-8")

    ok, stdout, stderr = ansible_run(playbook_path)

    state["ansible_stdout"] = stdout
    state["ansible_stderr"] = stderr
    state["current_step_index"] = idx + 1

    if ok:
        _db.execute(
            "UPDATE plan_steps SET status = 'succeeded', ansible_stdout = ?, ansible_stderr = ? WHERE plan_id = ? AND step_index = ?",
            (stdout, stderr, plan_id, idx),
        )
        _db.commit()
        total = len(plan.steps)
        if idx + 1 >= total:
            _db.execute("UPDATE plans SET status = 'completed' WHERE plan_id = ?", (plan_id,))
            _db.commit()
            state["status"] = "completed"
        else:
            state["status"] = f"step_{idx + 1}_of_{total}_succeeded"
    else:
        _db.execute(
            "UPDATE plan_steps SET status = 'failed', ansible_stdout = ?, ansible_stderr = ?, error = ? WHERE plan_id = ? AND step_index = ?",
            (stdout, stderr, stderr[:500], plan_id, idx),
        )
        _db.execute("UPDATE plans SET status = 'failed' WHERE plan_id = ?", (plan_id,))
        _db.commit()
        state["status"] = "failed"

    return state
