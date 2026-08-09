from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from orchestrator.child_bridge import ChildBridge
from orchestrator.db import connect, init_db
from orchestrator.graph import create_graphs
from orchestrator.models import AgentState
from orchestrator import nodes
from orchestrator.prompts import build_topology_context
from orchestrator.snapshot import SnapshotManager

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
ORCHESTRATOR_NODE_ID = os.environ.get("ORCHESTRATOR_NODE_ID", "vm1")

task_thread_map: dict[int, str] = {}

INSPECT_KEYWORDS = [
    "status of", "is", "running", "disk", "storage", "free space",
    "log", "capabilities", "readiness", "capacity", "health", "check",
]

MUTATION_KEYWORDS = [
    "install", "remove", "update", "write", "add ", "restart",
    "stop ", "start ", "delete", "create", "deploy", "config",
]


def detect_child_delegate(line: str, valid_node_ids: set[str]) -> tuple[str, str] | None:
    """Detect an inspection request targeted at a single node."""
    text = line.lower()
    if any(m in text for m in MUTATION_KEYWORDS):
        return None
    if not any(k in text for k in INSPECT_KEYWORDS):
        return None
    for node_id in sorted(valid_node_ids, key=len, reverse=True):
        if node_id in text:
            return (node_id, line)
    return None


def _delegate_flow(conn, bridge: ChildBridge, node_id: str, request: str) -> None:
    from orchestrator.nodes import create_child_task, plan_child_intent

    intent = plan_child_intent(request, node_id)
    if intent is None:
        print(f"  Could not plan a child action for: {request}")
        return
    print(f"  Delegating to {node_id}: {intent.action.value}")
    print(f"  Params: {json.dumps(intent.params, default=str)}")
    print(f"  Reason: {intent.reason}")
    try:
        choice = input("  Approve delegation? (y/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        choice = "n"
    if choice not in ("y", "yes"):
        print("  Delegation cancelled.")
        return
    task_id = create_child_task(conn, node_id, request, intent)
    print(f"  Task #{task_id} -> agent.{node_id}.tasks.request")
    if not bridge.is_connected():
        print(f"  Child bridge is not connected to NATS. Task #{task_id} remains pending; "
              f"start NATS and retry with /delegate.")
        return
    bridge.send_task(node_id, task_id, intent.model_dump_json())
    result = bridge.wait_for_result(task_id, timeout=120)
    if result is None:
        print(f"  No result within 120s (check /task {task_id} or /agents).")
        return
    status = result.get("status", "")
    print(f"  Result: {'OK' if result.get('ok') else 'FAILED'} ({status})")
    stdout = result.get("stdout") or ""
    if stdout:
        for lo in stdout.splitlines()[-8:]:
            print(f"    {lo}")
    if result.get("error"):
        print(f"    Error: {result['error']}")


def seed_inventory_nodes(conn) -> None:
    inv_path = Path(__file__).resolve().parent.parent / os.environ.get("ANSIBLE_INVENTORY", "ansible/inventory.ini")
    if not inv_path.exists():
        return
    with open(inv_path) as f:
        current_section = None
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current_section = line[1:-1]
                continue
            if current_section and (current_section.endswith(":vars") or current_section in ("lxcs", "vms")):
                continue
            parts = line.split()
            if not parts:
                continue
            hostname = parts[0]
            kwargs: dict[str, str] = {}
            for token in parts[1:]:
                if "=" in token:
                    k, _, v = token.partition("=")
                    kwargs[k] = v
            enabled = 1 if current_section == "managed_nodes" else 0
            conn.execute(
                "INSERT OR IGNORE INTO nodes (node_id, hostname, ip, node_type, role, enabled) VALUES (?, ?, ?, ?, ?, ?)",
                (hostname, hostname, kwargs.get("ansible_host", ""), kwargs.get("node_type", "unknown"), kwargs.get("role", "worker"), enabled),
            )
    conn.commit()


def fetch_nodes(conn) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM nodes ORDER BY node_id").fetchall()]


def fetch_telemetry(conn) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM telemetry_latest ORDER BY node_id").fetchall()]


def fetch_tasks(conn, limit: int = 10) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT task_id, user_request, target_node_id, status, error, created_at FROM tasks ORDER BY task_id DESC LIMIT ?", (limit,)).fetchall()]


def _fmt_size(bytes_val: int) -> str:
    if bytes_val >= 1073741824:
        return f"{bytes_val / 1073741824:.1f} GB"
    elif bytes_val >= 1048576:
        return f"{bytes_val / 1048576:.1f} MB"
    elif bytes_val >= 1024:
        return f"{bytes_val / 1024:.1f} KB"
    return f"{bytes_val} B"


def print_help() -> None:
    print("Commands:")
    print("  <prompt>         Natural language request")
    print("  /nodes           Show registered nodes with latest telemetry")
    print("  /topology        Show cluster topology from LLM perspective")
    print("  /history [n]     Show last n tasks (default 10)")
    print("  /task <id>       Show task details with per-step status")
    print("  /approve <id>    Approve and execute awaiting task")
    print("  /reject <id>     Reject awaiting task")
    print("  /freeform        Toggle freeform mode")
    print("  /rollback <id>   Rollback all snapshots for a task")
    print("  /snapshot list|free|delete|prune  Manage snapshots")
    print("  /agents          List nodes with child-agent presence")
    print("  /capabilities <node_id>  Show reported child-agent capabilities")
    print("  /delegate <node_id> <request>  Run an inspection task on a child agent")
    print("  /help            This message")
    print("  /exit            Exit")


def build_initial_state(user_input: str, topology: str, freeform: bool) -> AgentState:
    return {
        "user_input": user_input,
        "topology_context": topology,
        "freeform_mode": freeform,
        "intent": None,
        "task_id": None,
        "task_target": "",
        "playbook_path": None,
        "policy_ok": False,
        "syntax_ok": False,
        "plan": None,
        "current_step_index": 0,
        "ansible_stdout": "",
        "ansible_stderr": "",
        "error": None,
        "status": "",
        "authorization_ok": False,
    }


def main() -> None:
    global task_thread_map
    load_dotenv()
    conn = connect()
    init_db(conn)
    seed_inventory_nodes(conn)

    valid_node_ids = {r["node_id"] for r in conn.execute("SELECT node_id FROM nodes").fetchall()}
    snapshot_mgr = SnapshotManager(db=conn)

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "gemini/gemma-4-31b-it")
    nodes.init_nodes(conn, valid_node_ids, api_key, model)

    nats_url = os.environ.get("NATS_URL", NATS_URL)
    auth_secret = os.environ.get("CHILD_AUTH_SECRET", "")
    bridge = ChildBridge(
        nats_url,
        auth_secret,
        nats_user=os.environ.get("NATS_USER", ""),
        nats_password=os.environ.get("NATS_PASSWORD", ""),
        nats_creds_file=os.environ.get("NATS_CREDS_FILE", ""),
        source_node_id=os.environ.get("ORCHESTRATOR_NODE_ID", ORCHESTRATOR_NODE_ID),
    )
    bridge.start()

    checkpointer = MemorySaver()
    template_graph, freeform_graph = create_graphs(checkpointer, nodes)

    print(f"Connected to {nats_url}")
    print()
    if not api_key:
        print("Note: No LLM API key found. Planner will use fallback mode.")
        print()
    if not auth_secret:
        print("Note: No CHILD_AUTH_SECRET set. Child delegation will be rejected by agents.")
        print()

    print_help()
    print()

    freeform_mode = False
    current_graph = template_graph
    session_counter = 0

    while True:
        try:
            prompt = "[freeform] orchestrator> " if freeform_mode else "orchestrator> "
            line = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        if line == "/exit":
            break
        elif line == "/help":
            print_help()
        elif line == "/freeform":
            freeform_mode = not freeform_mode
            current_graph = freeform_graph if freeform_mode else template_graph
            print(f"Freeform mode: {'ON' if freeform_mode else 'OFF'}")
        elif line == "/nodes":
            for n in fetch_nodes(conn):
                t = next((x for x in fetch_telemetry(conn) if x["node_id"] == n["node_id"]), {})
                cpu = t.get("cpu_percent", "?")
                ram = t.get("ram_percent", "?")
                disk = t.get("disk_percent", "?")
                uptime = t.get("uptime_seconds", "?")
                print(f"  {n['node_id']:8s} {str(n.get('ip','?')):16s} {str(n.get('node_type','?')):6s} {str(n.get('role','?')):12s} {cpu!s:>7s} {ram!s:>6s} {disk!s:>6s} {uptime!s:>7s}")
        elif line == "/topology":
            nodes_list = fetch_nodes(conn)
            telemetry_raw = fetch_telemetry(conn)
            telemetry_clean = []
            for t in telemetry_raw:
                try:
                    telemetry_clean.append(json.loads(t["payload_json"]))
                except Exception:
                    telemetry_clean.append(t)
            print(build_topology_context([dict(n) for n in nodes_list], telemetry_clean))
        elif line.startswith("/history"):
            parts = line.split()
            limit = int(parts[1]) if len(parts) > 1 else 10
            for t in fetch_tasks(conn, limit):
                print(f"  #{t['task_id']:3d} | {t['status']:18s} | {str(t.get('user_request',''))[:50]:50s} | {str(t.get('target_node_id','')):15s} | {str(t.get('created_at',''))[:19]}")
        elif line.startswith("/task "):
            task_id = int(line.split()[1])
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                print(f"Task #{task_id} not found.")
                continue
            d = dict(row)
            print(f"Task #{d['task_id']}")
            print(f"  Request:   {d.get('user_request', '')}")
            print(f"  Target:    {d.get('target_node_id', '')}")
            print(f"  Status:    {d.get('status', '')}")
            if d.get("intent_json"):
                try:
                    print(f"  Intent:    {json.dumps(json.loads(d['intent_json']), indent=2)}")
                except Exception:
                    print(f"  Intent:    {d['intent_json']}")
            if d.get("error"):
                print(f"  Error:     {d['error']}")
            steps = conn.execute("SELECT * FROM task_steps WHERE task_id = ? ORDER BY step_index", (task_id,)).fetchall()
            if steps:
                print(f"  Steps ({len(steps)}):")
                for s in steps:
                    s = dict(s)
                    icon = "✓" if s["status"] == "succeeded" else "✗" if s["status"] == "failed" else "○" if s["status"] == "running" else "·"
                    desc = s.get("description", "") or ""
                    print(f"    {icon} {s['step_index']+1}: {desc} [{s['status']}]")
                    if s.get("ansible_stdout"):
                        for lo in s["ansible_stdout"].splitlines()[-5:]:
                            print(f"      {lo}")
                    if s.get("ansible_stderr"):
                        for lo in s["ansible_stderr"].splitlines()[-5:]:
                            print(f"      {lo}")
                    if s.get("error"):
                        print(f"      Error: {s['error']}")
            snaps = snapshot_mgr.get_by_task(task_id)
            if snaps:
                print(f"  Snapshots ({len(snaps)}):")
                for s in snaps:
                    print(f"    #{s['snapshot_id']} {s['node_id']} ({_fmt_size(s.get('size_bytes',0) or 0)}) [{s['status']}]")
        elif line.startswith("/plan "):
            plan_id = int(line.split()[1])
            plan_row = conn.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
            if not plan_row:
                print(f"Plan #{plan_id} not found.")
                continue
            print(f"Plan #{plan_row['plan_id']}: {plan_row['description']}")
            print(f"  Status:  {plan_row['status']}")
            steps = conn.execute("SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY step_index", (plan_id,)).fetchall()
            print(f"  Steps ({len(steps)}):")
            for s in steps:
                icon = "✓" if s["status"] == "succeeded" else "✗" if s["status"] == "failed" else "○" if s["status"] == "running" else "·"
                print(f"    {icon} {s['step_index']+1}: {s['description']} [{s['status']}]")
        elif line.startswith("/approve "):
            task_id = int(line.split()[1])
            tid = task_thread_map.get(task_id)
            if not tid:
                print(f"Task #{task_id} not found in active sessions.")
                continue
            config = {"configurable": {"thread_id": tid}}
            state = template_graph.get_state(config)
            if not state or not state.next:
                print(f"Task #{task_id} is not paused (next: {state.next if state else 'none'}).")
                continue
            print(f"Resuming task #{task_id}...")
            for event in template_graph.stream(Command(resume="approve"), config):
                if isinstance(event, dict):
                    for _, output in event.items():
                        if isinstance(output, dict):
                            if output.get("status"):
                                print(f"  Status: {output['status']}")
                            if output.get("ansible_stdout"):
                                for lo in output["ansible_stdout"].splitlines()[-10:]:
                                    print(f"  {lo}")
        elif line.startswith("/reject "):
            task_id = int(line.split()[1])
            tid = task_thread_map.get(task_id)
            if tid:
                config = {"configurable": {"thread_id": tid}}
                for event in template_graph.stream(Command(resume="reject"), config):
                    if isinstance(event, dict):
                        for _, output in event.items():
                            if isinstance(output, dict) and output.get("status"):
                                print(f"  Status: {output['status']}")
                print(f"Task #{task_id} rejected via graph.")
            else:
                conn.execute("UPDATE tasks SET status = ? WHERE task_id = ?", ("rejected", task_id))
                conn.commit()
                print(f"Task #{task_id} rejected directly.")
        elif line.startswith("/rollback "):
            parts = line.split()
            if len(parts) < 2:
                print("Usage: /rollback <task_id>")
                continue
            task_id = int(parts[1])
            succeeded_steps = conn.execute("SELECT step_index, description FROM task_steps WHERE task_id = ? AND status = 'succeeded' ORDER BY step_index DESC", (task_id,)).fetchall()
            snaps = snapshot_mgr.get_by_task(task_id)
            if not succeeded_steps and not snaps:
                print(f"No succeeded steps or snapshots available for rollback on task #{task_id}.")
                continue
            if succeeded_steps:
                print(f"Task #{task_id} — {len(succeeded_steps)} workflow step(s) to undo:")
                for s in succeeded_steps:
                    print(f"  - Step {s['step_index']+1}: {s['description']}")
            if snaps:
                total_size = sum(s.get("size_bytes", 0) or 0 for s in snaps)
                print(f"Task #{task_id} — {len(snaps)} snapshot(s), {_fmt_size(total_size)}")
                for s in snaps:
                    print(f"  Snapshot #{s['snapshot_id']}: {s['node_id']} ({_fmt_size(s.get('size_bytes',0) or 0)}) [{s['status']}]")
            try:
                choice = input("Approve rollback execution? (y/N): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = "n"
            if choice in ("y", "yes"):
                if succeeded_steps:
                    ok_rb, msg_rb = nodes.rollback_workflow_task(conn, task_id)
                    print(f"  Workflow rollback {'succeeded' if ok_rb else 'failed'}: {msg_rb}")
                if snaps:
                    for s in snaps:
                        if s["status"] != "created":
                            continue
                        result = snapshot_mgr.rollback(s["snapshot_id"])
                        print(f"  {'✓' if result.get('ok') else '✗'} Rolled back snapshot {s['node_id']}")
            else:
                print("Rollback cancelled.")
        elif line.startswith("/snapshot list"):
            for s in snapshot_mgr.get_all():
                task_req = ""
                task_id = s.get("task_id")
                if task_id:
                    tr = conn.execute("SELECT user_request FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                    if tr:
                        task_req = tr["user_request"][:30]
                req_str = f" #{task_id} {task_req}" if task_id else ""
                print(f"  {s['snapshot_id']:5d} {s['node_id']:8s}{req_str:40s} {(s.get('created_at','') or '?')[:16]:12s} {_fmt_size(s.get('size_bytes',0) or 0):>8s} {s['status']:14s}")
        elif line == "/snapshot free":
            total = 0
            for s in snapshot_mgr.get_node_summary():
                total += s["total_bytes"]
                print(f"  {s['node_id']:8s}  {s['count']:3d} snapshot(s)  {_fmt_size(s['total_bytes']):>8s}")
            print(f"  {'':8s}  {'':3s}              {'---------':>8s}")
            print(f"  {'TOTAL':8s}  {sum(s['count'] for s in snapshot_mgr.get_node_summary()):3d} snapshot(s)  {_fmt_size(total):>8s}")
        elif line.startswith("/snapshot delete "):
            snap_id = int(line.split()[2])
            row = conn.execute("SELECT * FROM snapshots WHERE snapshot_id = ?", (snap_id,)).fetchone()
            if not row:
                print(f"Snapshot #{snap_id} not found.")
                continue
            result = snapshot_mgr.delete(snap_id)
            print(f"  {'✓' if result.get('ok') else '✗'} Snapshot #{snap_id} deleted.")
        elif line.startswith("/snapshot prune"):
            parts = line.split()
            if len(parts) < 3:
                print("Usage: /snapshot prune <days>")
                continue
            result = snapshot_mgr.prune(int(parts[2]))
            print(f"  Deleted {result['deleted']} snapshot(s) older than {parts[2]} day(s).")
        elif line == "/agents":
            rows = [dict(r) for r in conn.execute(
                "SELECT node_id, agent_status, agent_version, agent_last_heartbeat, agent_capabilities FROM nodes ORDER BY node_id"
            ).fetchall()]
            active = False
            for n in rows:
                if not (n.get("agent_status") or n.get("agent_version") or n.get("agent_last_heartbeat") or n.get("agent_capabilities")):
                    continue
                active = True
                print(f"  {n['node_id']:8s} status={str(n.get('agent_status') or '?'):8s} v={str(n.get('agent_version') or '?'):8s} hb={str(n.get('agent_last_heartbeat') or '?')[:19]}")
                caps = n.get("agent_capabilities")
                if caps:
                    try:
                        c = json.loads(caps)
                        print(f"      os={c.get('os_family')} pkg={c.get('package_manager')} init={c.get('init_system')} actions={','.join(c.get('local_actions') or [])}")
                    except Exception:
                        pass
            if not active:
                print("  No child agents have reported yet.")
        elif line.startswith("/capabilities "):
            node_id = line.split()[1]
            row = conn.execute("SELECT agent_capabilities FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
            if not row or not row["agent_capabilities"]:
                print(f"  No capabilities reported for {node_id}.")
            else:
                try:
                    print(json.dumps(json.loads(row["agent_capabilities"]), indent=2))
                except Exception:
                    print(row["agent_capabilities"])
        elif line.startswith("/delegate "):
            parts = line.split(maxsplit=2)
            node_id = parts[1]
            request = parts[2] if len(parts) > 2 else ""
            if not request:
                print("Usage: /delegate <node_id> <request>")
                continue
            row = conn.execute("SELECT node_id FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
            if not row:
                print(f"  Unknown node: {node_id}")
                continue
            _delegate_flow(conn, bridge, node_id, request)
        else:
            routed = detect_child_delegate(line, valid_node_ids)
            if routed:
                node_id, request = routed
                print(f"Routing inspection request to child agent on {node_id}: {request}")
                _delegate_flow(conn, bridge, node_id, request)
                continue
            session_counter += 1
            thread_id = f"sess-{session_counter}"
            config = {"configurable": {"thread_id": thread_id}}

            nodes_list = fetch_nodes(conn)
            telemetry_raw = fetch_telemetry(conn)
            telemetry_clean = []
            for t in telemetry_raw:
                try:
                    telemetry_clean.append(json.loads(t["payload_json"]))
                except Exception:
                    telemetry_clean.append(t)
            topology = build_topology_context([dict(n) for n in nodes_list], telemetry_clean)

            print(f"Planning: {line}")

            try:
                first_event = True
                for event in current_graph.stream(build_initial_state(line, topology, freeform_mode), config):
                    if not isinstance(event, dict):
                        continue
                    for node_name, output in event.items():
                        if node_name == "__interrupt__":
                            state_vals = current_graph.get_state(config).values if current_graph.get_state(config) else {}
                            task_id = state_vals.get("task_id")
                            playbook_path = state_vals.get("playbook_path")
                            plan = state_vals.get("plan")

                            if plan and freeform_mode:
                                print(f"  Plan: {plan.description}")
                                for i, step in enumerate(plan.steps):
                                    print(f"    {i+1}/{len(plan.steps)}: {step.description}")
                            elif playbook_path:
                                print(f"  Playbook: {playbook_path}")
                                print(f"  Syntax check: OK")
                                try:
                                    content = Path(playbook_path).read_text()
                                    print("  ---")
                                    for pl in content.splitlines()[:15]:
                                        print(f"  {pl}")
                                    if len(content.splitlines()) > 15:
                                        print(f"  ... ({len(content.splitlines())} lines total)")
                                    print("  ---")
                                except Exception:
                                    pass

                            if task_id:
                                task_thread_map[task_id] = thread_id

                            try:
                                choice = input("  Approve this plan? (y/N): ").strip().lower()
                            except (EOFError, KeyboardInterrupt):
                                choice = "n"

                            resume_val = "approve" if choice in ("y", "yes") else "reject"

                            if resume_val == "reject":
                                if task_id:
                                    conn.execute("UPDATE task_steps SET status = 'skipped' WHERE task_id = ? AND status = 'pending'", (task_id,))
                                    conn.execute("UPDATE tasks SET status = 'rejected' WHERE task_id = ?", (task_id,))
                                    conn.commit()

                            for resume_event in current_graph.stream(Command(resume=resume_val), config):
                                if isinstance(resume_event, dict):
                                    for step_node, resume_output in resume_event.items():
                                        if isinstance(resume_output, dict):
                                            status = resume_output.get("status", "")
                                            if status == "rejected":
                                                print(f"  Workflow rejected.")
                                            elif status.startswith("step_"):
                                                steps_res = conn.execute("SELECT description FROM task_steps WHERE task_id = ? ORDER BY step_index", (task_id,)).fetchall() if task_id else []
                                                idx = resume_output.get("current_step_index", 0)
                                                desc = steps_res[idx - 1]["description"] if steps_res and idx <= len(steps_res) else ""
                                                print(f"  Step {idx} of {len(steps_res)}: {desc}")
                                                stdout = resume_output.get("ansible_stdout", "")
                                                if stdout:
                                                    for lo in stdout.splitlines()[-5:]:
                                                        print(f"    {lo}")
                                            elif status == "completed":
                                                print(f"  All steps completed.")
                                            elif status == "failed":
                                                print(f"  Step failed: {resume_output.get('error', 'unknown error')}")
                                                stdout = resume_output.get("ansible_stdout", "")
                                                if stdout:
                                                    for lo in stdout.splitlines()[-5:]:
                                                        print(f"    {lo}")
                                                stderr = resume_output.get("ansible_stderr", "")
                                                if stderr:
                                                    for lo in stderr.splitlines()[-5:]:
                                                        print(f"    {lo}")
                                                if task_id:
                                                    succeeded_steps = conn.execute("SELECT step_index, description FROM task_steps WHERE task_id = ? AND status = 'succeeded' ORDER BY step_index DESC", (task_id,)).fetchall()
                                                    if succeeded_steps:
                                                        print(f"\n  A rollback plan is available ({len(succeeded_steps)} succeeded step(s) to undo):")
                                                        for s in succeeded_steps:
                                                            print(f"    - Step {s['step_index']+1}: {s['description']}")
                                                        try:
                                                            rb_choice = input("  Execute rollback plan? (y/N): ").strip().lower()
                                                        except (EOFError, KeyboardInterrupt):
                                                            rb_choice = "n"
                                                        if rb_choice in ("y", "yes"):
                                                            ok_rb, msg_rb = nodes.rollback_workflow_task(conn, task_id)
                                                            print(f"  Rollback {'succeeded' if ok_rb else 'failed'}: {msg_rb}")
                                            if resume_output.get("status") not in ("", "rejected") and not status.startswith("step_"):
                                                if resume_output.get("ansible_stdout"):
                                                    for lo in resume_output["ansible_stdout"].splitlines()[-10:]:
                                                        print(f"  {lo}")
                        else:
                            if isinstance(output, dict) and first_event and not freeform_mode:
                                tid = output.get("task_id")
                                intent = output.get("intent")
                                if tid and intent:
                                    print(f"  Task #{tid}: {intent.action.value} on {intent.target.node_id or intent.target.group}")
                                    print(f"  Reason: {intent.reason}")
                                first_event = False
            except Exception as e:
                print(f"  Graph error: {e}")
            continue


if __name__ == "__main__":
    main()
