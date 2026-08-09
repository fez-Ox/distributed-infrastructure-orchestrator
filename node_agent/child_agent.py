"""Child agent service that runs on managed nodes.

A child agent reuses the exact same LangGraph loop and guardrails as the
orchestrator (validate -> render -> syntax_check -> execute), but:
  - never plans intents itself (intents arrive from the orchestrator),
  - only performs cataloged, policy-bounded actions from the child catalog,
  - only targets itself, and
  - skips the interactive approval prompt when a valid authorization token
    accompanies the task (approval happened at the orchestrator).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import nats
from dotenv import load_dotenv
from node_agent import telemetry_pub
from node_agent.child_catalog import CHILD_ACTIONS, render_child_playbook, validate_params
from orchestrator.child_token import verify_token
from orchestrator.child_protocol import (
    CapabilityReport,
    Heartbeat,
    ReadinessRequest,
    ReadinessResponse,
    StatusEvent,
    TaskAck,
    TaskCancel,
    TaskProgress,
    TaskRequest,
    TaskResult,
    encode_message,
)
from orchestrator.db import (
    child_cancel_requested,
    claim_child_task,
    connect,
    finish_child_task,
    init_db,
    request_child_cancel,
)
from orchestrator.executor import ansible_run, syntax_check
from orchestrator.models import Intent
from orchestrator.playbook_validator import validate_playbook

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
NODE_ID = os.environ.get("NODE_ID") or socket.gethostname().lower()
NODE_TYPE = os.environ.get("NODE_TYPE", "unknown")
NODE_ROLE = os.environ.get("NODE_ROLE", "worker")
CHILD_AUTH_SECRET = os.environ.get("CHILD_AUTH_SECRET", "")
NATS_USER = os.environ.get("NATS_USER", "")
NATS_PASSWORD = os.environ.get("NATS_PASSWORD", "")
NATS_CREDS_FILE = os.environ.get("NATS_CREDS_FILE", "")
ORCHESTRATOR_NODE_ID = os.environ.get("ORCHESTRATOR_NODE_ID", "vm1")
CHILD_DATABASE_PATH = os.environ.get("CHILD_DATABASE_PATH", "./data/child_state.db")
HEARTBEAT_INTERVAL = int(os.environ.get("CHILD_HEARTBEAT_INTERVAL_SECONDS", "15"))
CHILD_AGENT_VERSION = "0.2.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_local_capabilities() -> dict[str, object]:
    os_family = "unknown"
    package_manager = "unknown"
    init_system = "unknown"
    try:
        with open("/etc/os-release") as f:
            content = f.read()
        if "ID=alpine" in content:
            os_family, package_manager = "Alpine", "apk"
        elif "ID=debian" in content or "ID=ubuntu" in content:
            os_family, package_manager = "Debian", "apt"
        elif "ID=fedora" in content or "ID=rhel" in content or "ID=centos" in content:
            os_family, package_manager = "RedHat", "dnf"
    except FileNotFoundError:
        pass
    if shutil.which("systemctl"):
        init_system = "systemd"
    elif shutil.which("rc-service") or os.path.exists("/etc/init.d"):
        init_system = "openrc"
    return {
        "node_id": NODE_ID,
        "os_family": os_family,
        "package_manager": package_manager,
        "init_system": init_system,
        "local_actions": sorted(a.value for a in CHILD_ACTIONS),
        "agent_version": CHILD_AGENT_VERSION,
    }


def init_local_db():
    db = connect(CHILD_DATABASE_PATH)
    init_db(db)
    try:
        ip = telemetry_pub.local_ip()
    except Exception:
        ip = ""
    db.execute(
        "INSERT OR REPLACE INTO nodes (node_id, hostname, ip, node_type, role, enabled, last_seen, agent_status, agent_version) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (NODE_ID, socket.gethostname(), ip, NODE_TYPE, NODE_ROLE, _now(), "online", CHILD_AGENT_VERSION),
    )
    db.commit()
    return db


def _build_state(intent: Intent, task_id: int) -> dict[str, object]:
    return {
        "user_input": intent.reason or "",
        "topology_context": "",
        "freeform_mode": False,
        "intent": intent,
        "task_id": task_id,
        "task_target": intent.target.node_id or intent.target.group or "",
        "playbook_path": None,
        "policy_ok": False,
        "syntax_ok": False,
        "plan": None,
        "current_step_index": 0,
        "ansible_stdout": "",
        "ansible_stderr": "",
        "error": None,
        "status": "",
        "authorization_ok": True,
    }


def evaluate_local_readiness(workload: str, constraints: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Evaluate only declared resource constraints; never execute workload code."""
    disk = shutil.disk_usage("/")
    capacity: dict[str, Any] = {
        "free_disk_bytes": disk.free,
        "cpu_count": os.cpu_count() or 1,
    }
    min_free = constraints.get("min_free_disk_bytes", 0)
    max_load = constraints.get("max_load_per_cpu")
    if not isinstance(min_free, int) or min_free < 0 or min_free > 1 << 50:
        return False, "invalid min_free_disk_bytes constraint", capacity
    if not isinstance(max_load, (int, float)) or max_load < 0 or max_load > 100:
        if max_load is not None:
            return False, "invalid max_load_per_cpu constraint", capacity
    if disk.free < min_free:
        return False, "insufficient free disk", capacity
    if max_load is not None and hasattr(os, "getloadavg"):
        load_per_cpu = os.getloadavg()[0] / max(1, os.cpu_count() or 1)
        capacity["load_per_cpu"] = load_per_cpu
        if load_per_cpu > float(max_load):
            return False, "current load exceeds requested limit", capacity
    return True, f"node is ready for workload {workload}", capacity


def _seed_local_task(db, task_id: int, request: str, intent: Intent) -> None:
    db.execute(
        "INSERT OR IGNORE INTO tasks (task_id, user_request, task_type, intent_json, target_node_id, status, execution_mode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, request, "child", intent.model_dump_json(), intent.target.node_id or intent.target.group or "",
         "pending", "child"),
    )
    db.execute(
        "INSERT OR IGNORE INTO task_steps (task_id, step_index, description, playbook_yaml, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, 0, request or "child task", "", "pending"),
    )
    db.commit()


def process_task(db, child_graph, request: TaskRequest) -> dict[str, object]:
    tag = f"child[{NODE_ID}]"
    task_id = request.task_id
    try:
        intent = Intent.model_validate_json(request.intent_json)
    except Exception as exc:
        print(f"  {tag} ✗ invalid intent for task {task_id}: {exc}", flush=True)
        return {"ok": False, "status": "", "stdout": "", "stderr": "", "error": f"invalid intent: {exc}"}

    claimed, prior_result = claim_child_task(db, task_id, NODE_ID, request.idempotency_key)
    if not claimed:
        if prior_result:
            return json.loads(prior_result)
        return {"ok": False, "status": "rejected", "stdout": "", "stderr": "", "error": "duplicate task is already running"}

    print(f"  {tag} → received task {task_id} ({intent.action.value}); verifying authorization token", flush=True)
    if not verify_token(CHILD_AUTH_SECRET, request.authorization_token, task_id, NODE_ID):
        result = {"ok": False, "status": "rejected", "stdout": "", "stderr": "", "error": "invalid or expired authorization token"}
        finish_child_task(db, task_id, NODE_ID, "rejected", json.dumps(result))
        print(f"  {tag} ✗ task {task_id} rejected: invalid or expired token", flush=True)
        return result

    cancelled, cancel_reason = child_cancel_requested(db, task_id, NODE_ID)
    if cancelled:
        result = {"ok": False, "status": "rejected", "stdout": "", "stderr": "", "error": cancel_reason or "task cancelled"}
        finish_child_task(db, task_id, NODE_ID, "rejected", json.dumps(result))
        return result

    print(f"  {tag} ✓ token valid; executing task {task_id} (action: {intent.action.value})", flush=True)
    _seed_local_task(db, task_id, intent.reason or "", intent)

    if intent.target.node_id not in {NODE_ID, "localhost"} or intent.action not in CHILD_ACTIONS:
        result = {"ok": False, "status": "rejected", "stdout": "", "stderr": "", "error": "intent is not allowed for this child node"}
        finish_child_task(db, task_id, NODE_ID, "rejected", json.dumps(result))
        return result
    param_errors = validate_params(intent.action, intent.params or {})
    if param_errors:
        result = {"ok": False, "status": "rejected", "stdout": "", "stderr": "", "error": "; ".join(param_errors)}
        finish_child_task(db, task_id, NODE_ID, "rejected", json.dumps(result))
        return result
    try:
        playbook_yaml = render_child_playbook(intent)
        validation_errors = validate_playbook(playbook_yaml)
        if validation_errors:
            raise ValueError("playbook validation failed: " + "; ".join(validation_errors))
        playbook_path = Path(CHILD_DATABASE_PATH).parent / f"child-task-{task_id}.yml"
        playbook_path.write_text(playbook_yaml, encoding="utf-8")
        syntax_ok, syntax_stdout, syntax_stderr = syntax_check(playbook_path, local=True)
        if not syntax_ok:
            result = {"ok": False, "status": "failed", "stdout": syntax_stdout, "stderr": syntax_stderr, "error": "child playbook syntax check failed"}
        else:
            ok, stdout, stderr = ansible_run(playbook_path, local=True)
            result = {"ok": ok, "status": "succeeded" if ok else "failed", "stdout": stdout, "stderr": stderr, "error": None if ok else "child playbook execution failed"}
    except Exception as exc:
        result = {"ok": False, "status": "failed", "stdout": "", "stderr": "", "error": str(exc)}
    cancelled, cancel_reason = child_cancel_requested(db, task_id, NODE_ID)
    if cancelled:
        result["ok"] = False
        result["status"] = "rejected"
        result["error"] = cancel_reason or "task cancelled"
    status = str(result["status"])
    finish_child_task(db, task_id, NODE_ID, result["status"] or "failed", json.dumps(result))
    outcome = "succeeded" if result["ok"] else f"failed/rejected ({status or 'error'})"
    print(f"  {tag} → task {task_id} {outcome}", flush=True)
    return result


async def run() -> None:
    db = init_local_db()
    child_graph = None

    connect_kwargs: dict[str, object] = {}
    if NATS_CREDS_FILE:
        connect_kwargs["user_credentials"] = NATS_CREDS_FILE
    elif NATS_USER:
        connect_kwargs.update(user=NATS_USER, password=NATS_PASSWORD)
    nc = await nats.connect(NATS_URL, **connect_kwargs)
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=2)

    async def handle_task(msg) -> None:
        request: TaskRequest | None = None
        try:
            request = TaskRequest.model_validate_json(msg.data)
            if request.node_id != NODE_ID:
                raise ValueError("task is addressed to another node")
            if request.expires_at <= datetime.now(timezone.utc):
                await nc.publish(
                    f"agent.{NODE_ID}.tasks.ack",
                    encode_message(TaskAck(node_id=NODE_ID, task_id=request.task_id, idempotency_key=request.idempotency_key, accepted=False, reason="expired")),
                )
                await nc.publish(
                    f"agent.{NODE_ID}.tasks.result",
                    encode_message(TaskResult(node_id=NODE_ID, task_id=request.task_id, idempotency_key=request.idempotency_key, status="expired", ok=False, error="task request expired before execution")),
                )
                return
            await nc.publish(
                f"agent.{NODE_ID}.tasks.ack",
                encode_message(TaskAck(node_id=NODE_ID, task_id=request.task_id, idempotency_key=request.idempotency_key, accepted=True, reason="accepted")),
            )
            await nc.publish(
                f"agent.{NODE_ID}.tasks.progress",
                encode_message(TaskProgress(node_id=NODE_ID, task_id=request.task_id, idempotency_key=request.idempotency_key, status="queued")),
            )
            await nc.publish(
                f"agent.{NODE_ID}.tasks.progress",
                encode_message(TaskProgress(node_id=NODE_ID, task_id=request.task_id, idempotency_key=request.idempotency_key, status="running")),
            )
            result = await loop.run_in_executor(executor, process_task, db, child_graph, request)
        except Exception as exc:
            result = {"ok": False, "status": "rejected", "stdout": "", "stderr": "", "error": f"invalid request: {exc}"}
        if request is None:
            print(f"  child[{NODE_ID}] rejected malformed task request: {result.get('error')}", flush=True)
            return
        if result.get("status") == "succeeded":
            await nc.publish(
                f"agent.{NODE_ID}.tasks.progress",
                encode_message(TaskProgress(node_id=NODE_ID, task_id=request.task_id, idempotency_key=request.idempotency_key, status="verifying")),
            )
        result_message = TaskResult(
            node_id=NODE_ID,
            task_id=request.task_id,
            idempotency_key=request.idempotency_key,
            status=result.get("status") if result.get("status") in {"succeeded", "failed", "rejected", "expired"} else "failed",
            ok=bool(result.get("ok")),
            stdout=str(result.get("stdout") or "")[:100_000],
            stderr=str(result.get("stderr") or "")[:100_000],
            error=str(result.get("error"))[:2_000] if result.get("error") else None,
        )
        await nc.publish(f"agent.{NODE_ID}.tasks.result", encode_message(result_message))

    async def handle_cancel(msg) -> None:
        try:
            request = TaskCancel.model_validate_json(msg.data)
            if request.node_id != NODE_ID or request.source_node_id != ORCHESTRATOR_NODE_ID:
                raise ValueError("unauthorized cancellation source or target")
            cancelled = request_child_cancel(db, request.task_id, NODE_ID, request.reason)
            if cancelled:
                await nc.publish(
                    f"agent.{NODE_ID}.tasks.progress",
                    encode_message(TaskProgress(node_id=NODE_ID, task_id=request.task_id, idempotency_key=request.idempotency_key, status="cancelling", detail=request.reason)),
                )
        except Exception as exc:
            print(f"  child[{NODE_ID}] rejected cancellation: {exc}", flush=True)

    async def handle_readiness(msg) -> None:
        try:
            request = ReadinessRequest.model_validate_json(msg.data)
            if request.node_id != NODE_ID or request.source_node_id != ORCHESTRATOR_NODE_ID:
                raise ValueError("unauthorized readiness source or target")
            ready, reason, capacity = evaluate_local_readiness(request.workload, request.constraints)
            response = ReadinessResponse(node_id=NODE_ID, request_id=request.request_id, workload=request.workload, ready=ready, reason=reason, capacity=capacity)
            await nc.publish(f"agent.{NODE_ID}.workload.readiness.response", encode_message(response))
        except Exception as exc:
            print(f"  child[{NODE_ID}] rejected readiness request: {exc}", flush=True)

    await nc.subscribe(f"agent.{NODE_ID}.tasks.request", cb=handle_task)
    await nc.subscribe(f"agent.{NODE_ID}.tasks.cancel", cb=handle_cancel)
    await nc.subscribe(f"agent.{NODE_ID}.workload.readiness.request", cb=handle_readiness)

    capabilities = detect_local_capabilities()
    await nc.publish(f"agent.{NODE_ID}.capabilities", encode_message(CapabilityReport(**capabilities)))
    await nc.publish(f"agent.{NODE_ID}.status", encode_message(StatusEvent(node_id=NODE_ID, status="online")))

    async def heartbeat() -> None:
        while True:
            await nc.publish(
                f"agent.{NODE_ID}.heartbeat",
                encode_message(Heartbeat(node_id=NODE_ID, agent_version=CHILD_AGENT_VERSION)),
            )
            # Republish capabilities so a restarted orchestrator can converge
            # without requiring a child-agent restart.
            await nc.publish(
                f"agent.{NODE_ID}.capabilities",
                encode_message(CapabilityReport(**detect_local_capabilities())),
            )
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    asyncio.create_task(heartbeat())
    print(f"child agent {NODE_ID} listening on {NATS_URL}")
    await asyncio.Event().wait()


def main() -> None:
    load_dotenv()
    asyncio.run(run())


if __name__ == "__main__":
    main()
