import pytest
from langgraph.checkpoint.memory import MemorySaver

from orchestrator.child_bridge import ChildBridge
from orchestrator.db import connect, init_db
from orchestrator.models import ActionType, Intent, NodeSelector
from orchestrator.policy import PolicyEngine, PolicyViolation
from orchestrator.preflight import PreflightEngine
from node_agent.child_catalog import CHILD_ACTIONS


def _intent(action, node_id="lxc1", params=None):
    return Intent(
        action=action,
        target=NodeSelector(node_id=node_id),
        params=params or {},
        reason="test",
    )


class TestChildPolicy:
    def test_rejects_mutating_action(self):
        engine = PolicyEngine(
            valid_node_ids={"lxc1"},
            mode="child",
            child_node_id="lxc1",
            allowed_actions=CHILD_ACTIONS,
        )
        with pytest.raises(PolicyViolation):
            engine.validate(_intent(ActionType.install_pkg, params={"package": "htop"}))

    def test_accepts_inspection_action(self):
        engine = PolicyEngine(
            valid_node_ids={"lxc1"},
            mode="child",
            child_node_id="lxc1",
            allowed_actions=CHILD_ACTIONS,
        )
        engine.validate(_intent(ActionType.service_status, params={"service": "nginx"}))

    def test_rejects_other_node(self):
        engine = PolicyEngine(
            valid_node_ids={"lxc1"},
            mode="child",
            child_node_id="lxc1",
            allowed_actions=CHILD_ACTIONS,
        )
        with pytest.raises(PolicyViolation):
            engine.validate(_intent(ActionType.service_status, node_id="lxc2", params={"service": "nginx"}))

    def test_rejects_unknown_action(self):
        engine = PolicyEngine(
            valid_node_ids={"lxc1"},
            mode="child",
            child_node_id="lxc1",
            allowed_actions=CHILD_ACTIONS,
        )
        with pytest.raises(PolicyViolation):
            engine.validate(_intent(ActionType.freeform))


class TestChildPreflight:
    def test_rejects_other_node(self):
        engine = PreflightEngine(None, mode="child", child_node_id="lxc1")
        errors = engine.check_all("lxc2", ActionType.service_status)
        assert errors

    def test_accepts_self(self):
        engine = PreflightEngine(None, mode="child", child_node_id="lxc1")
        assert engine.check_all("lxc1", ActionType.service_status) == []

    def test_accepts_localhost(self):
        engine = PreflightEngine(None, mode="child", child_node_id="lxc1")
        assert engine.check_all("localhost", ActionType.service_status) == []


class TestChildGraph:
    def _setup(self, tmp_path):
        db_path = tmp_path / "child.db"
        db = connect(str(db_path))
        init_db(db)
        db.execute(
            "INSERT INTO nodes (node_id, hostname, ip, node_type, role, enabled) VALUES (?,?,?,?,?,1)",
            ("lxc1", "lxc1", "10.0.0.11", "lxc", "worker"),
        )
        db.commit()
        return db

    def test_child_graph_compiles_without_plan(self, tmp_path):
        db = self._setup(tmp_path)
        from orchestrator import nodes
        from orchestrator.graph import create_child_graph

        nodes.init_nodes(
            db, {"lxc1", "localhost"}, api_key="", skip_preflight=True,
            mode="child", child_node_id="lxc1",
        )
        graph = create_child_graph(MemorySaver(), nodes)
        assert graph.name == "child_graph"
        assert "validate" in graph.nodes
        assert "render" in graph.nodes
        assert "syntax_check" in graph.nodes
        assert "execute" in graph.nodes
        assert "plan" not in graph.nodes

    def test_child_graph_runs_to_success(self, tmp_path, monkeypatch):
        from orchestrator import nodes
        from orchestrator.graph import create_child_graph

        db = self._setup(tmp_path)
        nodes.init_nodes(
            db, {"lxc1", "localhost"}, api_key="", skip_preflight=True,
            mode="child", child_node_id="lxc1",
        )
        monkeypatch.setattr(
            nodes, "ansible_run",
            lambda playbook_path, local=False: (True, "active\nok stdout", ""),
        )
        monkeypatch.setattr(
            nodes, "syntax_check",
            lambda playbook_path, local=False: (True, "ok", ""),
        )

        task_id = 7
        db.execute(
            "INSERT INTO tasks (task_id, user_request, task_type, status, execution_mode) "
            "VALUES (?,?,?,?,?)",
            (task_id, "check nginx", "child", "pending", "child"),
        )
        db.execute(
            "INSERT INTO task_steps (task_id, step_index, description, playbook_yaml, status) "
            "VALUES (?,?,?,?,?)",
            (task_id, 0, "check nginx", "", "pending"),
        )
        db.commit()

        graph = create_child_graph(MemorySaver(), nodes)
        state = {
            "user_input": "check nginx",
            "topology_context": "",
            "freeform_mode": False,
            "intent": _intent(ActionType.service_status, params={"service": "nginx"}),
            "task_id": task_id,
            "task_target": "lxc1",
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
        config = {"configurable": {"thread_id": "child-1"}}
        for _ in graph.stream(state, config):
            pass

        final = graph.get_state(config)
        assert final.values["status"] == "succeeded"

        row = db.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        assert row["status"] == "succeeded"

    def test_child_graph_rejects_without_auth(self, tmp_path, monkeypatch):
        from orchestrator import nodes
        from orchestrator.graph import create_child_graph

        db = self._setup(tmp_path)
        nodes.init_nodes(
            db, {"lxc1", "localhost"}, api_key="", skip_preflight=True,
            mode="child", child_node_id="lxc1",
        )
        task_id = 8
        db.execute(
            "INSERT INTO tasks (task_id, user_request, task_type, status, execution_mode) "
            "VALUES (?,?,?,?,?)",
            (task_id, "check nginx", "child", "pending", "child"),
        )
        db.execute(
            "INSERT INTO task_steps (task_id, step_index, description, playbook_yaml, status) "
            "VALUES (?,?,?,?,?)",
            (task_id, 0, "check nginx", "", "pending"),
        )
        db.commit()

        graph = create_child_graph(MemorySaver(), nodes)
        state = {
            "user_input": "check nginx",
            "topology_context": "",
            "freeform_mode": False,
            "intent": _intent(ActionType.service_status, params={"service": "nginx"}),
            "task_id": task_id,
            "task_target": "lxc1",
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
        config = {"configurable": {"thread_id": "child-noauth"}}
        for _ in graph.stream(state, config):
            pass

        final = graph.get_state(config)
        assert final.values["status"] == "rejected"


class TestChildPlanner:
    def test_fallback_child_intent_service_status(self):
        from orchestrator.nodes import _fallback_child_intent

        intent = _fallback_child_intent("is nginx running", "lxc1")
        assert intent is not None
        assert intent.action == ActionType.service_status
        assert intent.target.node_id == "lxc1"
        assert intent.params["service"] == "nginx"

    def test_fallback_child_intent_disk_check(self):
        from orchestrator.nodes import _fallback_child_intent

        intent = _fallback_child_intent("how much free space", "vm2")
        assert intent is not None
        assert intent.action == ActionType.disk_check
        assert intent.target.node_id == "vm2"

    def test_fallback_child_intent_unknown(self):
        from orchestrator.nodes import _fallback_child_intent

        assert _fallback_child_intent("install htop", "lxc1") is None


class TestChildBridge:
    def test_upsert_readiness_persists_state(self, tmp_path):
        db_path = tmp_path / "orch.db"
        conn = connect(str(db_path))
        init_db(conn)
        conn.close()

        bridge = ChildBridge("nats://127.0.0.1:1", "secret", db_path=str(db_path))
        bridge._upsert_readiness({
            "node_id": "lxc1", "ready": True, "reason": "enough capacity",
            "timestamp": "2026-01-01T00:00:00+00:00", "capacity": {},
        })
        conn = connect(str(db_path))
        node = conn.execute("SELECT * FROM nodes WHERE node_id = 'lxc1'").fetchone()
        assert node["agent_readiness"] == "ready"
        assert node["agent_readiness_reason"] == "enough capacity"
        conn.close()

    def test_upsert_ack_and_progress_update_task_state(self, tmp_path):
        db_path = tmp_path / "orch.db"
        conn = connect(str(db_path))
        init_db(conn)
        conn.execute("INSERT INTO tasks (task_id, user_request, task_type, status) VALUES (5, 'check', 'child', 'pending')")
        conn.execute("INSERT INTO task_steps (task_id, step_index, description, playbook_yaml, status) VALUES (5, 0, 'check', '', 'pending')")
        conn.execute("INSERT INTO child_task_runs (task_id, node_id, idempotency_key, status) VALUES (5, 'lxc1', 'child-task-5-lxc1', 'dispatched')")
        conn.commit()
        conn.close()
        bridge = ChildBridge("nats://127.0.0.1:1", "secret", db_path=str(db_path))
        bridge._upsert_task_event({"task_id": 5, "node_id": "lxc1", "idempotency_key": "child-task-5-lxc1", "accepted": True, "reason": None}, "ack")
        bridge._upsert_task_event({"task_id": 5, "node_id": "lxc1", "idempotency_key": "child-task-5-lxc1", "status": "verifying", "detail": "checking"}, "progress")
        conn = connect(str(db_path))
        assert conn.execute("SELECT status FROM tasks WHERE task_id = 5").fetchone()[0] == "verifying"
        conn.close()

    def test_upsert_result_updates_task(self, tmp_path):
        db_path = tmp_path / "orch.db"
        conn = connect(str(db_path))
        init_db(conn)
        tid = conn.execute(
            "INSERT INTO tasks (task_id, user_request, task_type, status, execution_mode) "
            "VALUES (?,?,?,?,?)",
            (5, "check nginx", "child", "pending", "child"),
        ).lastrowid
        conn.execute(
            "INSERT INTO task_steps (task_id, step_index, description, playbook_yaml, status) "
            "VALUES (?,?,?,?,?)",
            (tid, 0, "check nginx", "", "pending"),
        )
        conn.execute(
            "INSERT INTO child_task_runs (task_id, node_id, idempotency_key, status) VALUES (?, ?, ?, 'running')",
            (tid, "lxc1", "child-task-5-lxc1"),
        )
        conn.commit()
        conn.close()

        bridge = ChildBridge("nats://127.0.0.1:1", "secret", db_path=str(db_path))
        bridge._upsert_result(
            {"task_id": tid, "node_id": "lxc1", "idempotency_key": "child-task-5-lxc1", "ok": True, "status": "succeeded",
             "stdout": "active", "stderr": "", "error": None}
        )

        conn = connect(str(db_path))
        task = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (tid,)).fetchone()
        assert task["status"] == "succeeded"
        assert task["execution_mode"] == "child"
        step = conn.execute(
            "SELECT * FROM task_steps WHERE task_id = ? AND step_index = 0", (tid,)
        ).fetchone()
        assert step["status"] == "succeeded"
        assert step["ansible_stdout"] == "active"
        conn.close()
