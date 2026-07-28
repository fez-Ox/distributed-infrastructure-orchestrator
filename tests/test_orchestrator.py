import json

import pytest
from pydantic import ValidationError

from orchestrator.models import ActionType, Intent, NodeSelector, TaskStatus
from orchestrator.policy import PolicyEngine, PolicyViolation
from orchestrator.renderer import render_playbook


class TestModels:
    def test_valid_install_pkg_intent(self):
        intent = Intent(
            action=ActionType.install_pkg,
            target=NodeSelector(node_id="lxc1"),
            params={"package": "htop"},
            reason="Need htop for monitoring",
        )
        assert intent.action == ActionType.install_pkg
        assert intent.target.node_id == "lxc1"
        assert intent.params["package"] == "htop"
        pm = intent.params_model()
        assert pm.package == "htop"
        assert pm.state == "present"

    def test_valid_add_user_intent(self):
        intent = Intent(
            action=ActionType.add_user,
            target=NodeSelector(group="lxcs"),
            params={"username": "deploy", "groups": ["wheel"], "shell": "/bin/bash"},
            reason="Create deploy user",
        )
        pm = intent.params_model()
        assert pm.username == "deploy"
        assert pm.groups == ["wheel"]

    def test_valid_config_file_intent(self):
        intent = Intent(
            action=ActionType.config_file,
            target=NodeSelector(node_id="vm2"),
            params={"path": "/etc/issue", "content": "Welcome\n", "mode": "0644"},
            reason="Set login banner",
        )
        pm = intent.params_model()
        assert pm.path == "/etc/issue"
        assert pm.content == "Welcome\n"

    def test_valid_restart_service_intent(self):
        intent = Intent(
            action=ActionType.restart_service,
            target=NodeSelector(node_id="vm1"),
            params={"service": "sshd", "state": "restarted"},
            reason="Restart SSH",
        )
        pm = intent.params_model()
        assert pm.service == "sshd"

    def test_invalid_action(self):
        with pytest.raises(ValidationError):
            Intent(
                action="bad-action",
                target=NodeSelector(node_id="lxc1"),
                params={},
                reason="test",
            )

    def test_intent_requires_reason(self):
        with pytest.raises(ValidationError):
            Intent(
                action=ActionType.update_system,
                target=NodeSelector(group="managed_nodes"),
                params={},
            )

    def test_intent_json_roundtrip(self):
        original = Intent(
            action=ActionType.update_system,
            target=NodeSelector(group="managed_nodes"),
            params={},
            reason="Update all nodes",
        )
        data = json.loads(original.model_dump_json())
        restored = Intent.model_validate(data)
        assert restored.action == original.action
        assert restored.target.group == "managed_nodes"


class TestPolicy:
    def test_valid_intent_passes(self):
        engine = PolicyEngine(valid_node_ids={"lxc1", "lxc2", "vm1", "vm2"})
        intent = Intent(
            action=ActionType.install_pkg,
            target=NodeSelector(node_id="lxc1"),
            params={"package": "htop"},
            reason="test",
        )
        engine.validate(intent)

    def test_unknown_node_fails(self):
        engine = PolicyEngine(valid_node_ids={"lxc1", "lxc2"})
        intent = Intent(
            action=ActionType.install_pkg,
            target=NodeSelector(node_id="nonexistent"),
            params={"package": "htop"},
            reason="test",
        )
        with pytest.raises(PolicyViolation, match="unknown node_id"):
            engine.validate(intent)

    def test_unknown_group_fails(self):
        engine = PolicyEngine(valid_node_ids={"lxc1"})
        intent = Intent(
            action=ActionType.install_pkg,
            target=NodeSelector(group="bad_group"),
            params={"package": "htop"},
            reason="test",
        )
        with pytest.raises(PolicyViolation, match="unknown group"):
            engine.validate(intent)

    def test_no_target_fails(self):
        engine = PolicyEngine(valid_node_ids={"lxc1"})
        intent = Intent(
            action=ActionType.install_pkg,
            target=NodeSelector(),
            params={"package": "htop"},
            reason="test",
        )
        with pytest.raises(PolicyViolation, match="must specify"):
            engine.validate(intent)

    def test_install_pkg_missing_package(self):
        engine = PolicyEngine(valid_node_ids={"lxc1"})
        intent = Intent(
            action=ActionType.install_pkg,
            target=NodeSelector(node_id="lxc1"),
            params={},
            reason="test",
        )
        with pytest.raises(PolicyViolation, match="requires 'package'"):
            engine.validate(intent)

    def test_bad_package_state(self):
        engine = PolicyEngine(valid_node_ids={"lxc1"})
        intent = Intent(
            action=ActionType.install_pkg,
            target=NodeSelector(node_id="lxc1"),
            params={"package": "htop", "state": "burninated"},
            reason="test",
        )
        with pytest.raises(PolicyViolation, match="invalid package state"):
            engine.validate(intent)

    def test_add_user_missing_username(self):
        engine = PolicyEngine(valid_node_ids={"lxc1"})
        intent = Intent(
            action=ActionType.add_user,
            target=NodeSelector(node_id="lxc1"),
            params={},
            reason="test",
        )
        with pytest.raises(PolicyViolation, match="requires 'username'"):
            engine.validate(intent)

    def test_config_file_missing_path(self):
        engine = PolicyEngine(valid_node_ids={"lxc1"})
        intent = Intent(
            action=ActionType.config_file,
            target=NodeSelector(node_id="lxc1"),
            params={"content": "data"},
            reason="test",
        )
        with pytest.raises(PolicyViolation, match="requires 'path'"):
            engine.validate(intent)

    def test_restart_service_bad_state(self):
        engine = PolicyEngine(valid_node_ids={"lxc1"})
        intent = Intent(
            action=ActionType.restart_service,
            target=NodeSelector(node_id="lxc1"),
            params={"service": "nginx", "state": "invalid"},
            reason="test",
        )
        with pytest.raises(PolicyViolation, match="invalid service state"):
            engine.validate(intent)

    @pytest.mark.parametrize(
        "current,next_status,allowed",
        [
            (TaskStatus.pending, TaskStatus.planning, True),
            (TaskStatus.planning, TaskStatus.awaiting_approval, True),
            (TaskStatus.planning, TaskStatus.failed, True),
            (TaskStatus.awaiting_approval, TaskStatus.running, True),
            (TaskStatus.awaiting_approval, TaskStatus.rejected, True),
            (TaskStatus.running, TaskStatus.verifying, True),
            (TaskStatus.running, TaskStatus.failed, True),
            (TaskStatus.verifying, TaskStatus.succeeded, True),
            (TaskStatus.verifying, TaskStatus.failed, True),
            (TaskStatus.pending, TaskStatus.succeeded, False),
            (TaskStatus.succeeded, TaskStatus.running, False),
            (TaskStatus.rejected, TaskStatus.running, False),
        ],
    )
    def test_task_transitions(self, current, next_status, allowed):
        engine = PolicyEngine(valid_node_ids={"lxc1"})
        assert engine.allowed_transition(current, next_status) == allowed


class TestRenderer:
    def test_render_install_pkg(self):
        intent = Intent(
            action=ActionType.install_pkg,
            target=NodeSelector(node_id="lxc1"),
            params={"package": "htop"},
            reason="test",
        )
        playbook = render_playbook(intent)
        assert "lxc1" in playbook
        assert "htop" in playbook
        assert "ansible.builtin.package" in playbook
        assert "present" in playbook

    def test_render_update_system(self):
        intent = Intent(
            action=ActionType.update_system,
            target=NodeSelector(node_id="vm1"),
            params={},
            reason="test",
        )
        playbook = render_playbook(intent)
        assert "vm1" in playbook
        assert "ansible.builtin.apt" in playbook
        assert "ansible.builtin.command" in playbook

    def test_render_add_user(self):
        intent = Intent(
            action=ActionType.add_user,
            target=NodeSelector(group="lxcs"),
            params={"username": "deploy", "groups": ["wheel"], "shell": "/bin/bash"},
            reason="test",
        )
        playbook = render_playbook(intent)
        assert "lxcs" in playbook
        assert "deploy" in playbook
        assert "wheel" in playbook
        assert "/bin/bash" in playbook

    def test_render_add_user_with_ssh_key(self):
        intent = Intent(
            action=ActionType.add_user,
            target=NodeSelector(node_id="lxc1"),
            params={
                "username": "deploy",
                "ssh_key": "ssh-ed25519 AAAAC3... user@host",
            },
            reason="test",
        )
        playbook = render_playbook(intent)
        assert "authorized_key" in playbook
        assert "ssh-ed25519" in playbook

    def test_render_config_file(self):
        intent = Intent(
            action=ActionType.config_file,
            target=NodeSelector(node_id="vm2"),
            params={"path": "/etc/motd", "content": "Welcome!\n"},
            reason="test",
        )
        playbook = render_playbook(intent)
        assert "vm2" in playbook
        assert "/etc/motd" in playbook
        assert "Welcome!" in playbook

    def test_render_restart_service(self):
        intent = Intent(
            action=ActionType.restart_service,
            target=NodeSelector(node_id="vm1"),
            params={"service": "nginx", "state": "restarted", "enabled": True},
            reason="test",
        )
        playbook = render_playbook(intent)
        assert "vm1" in playbook
        assert "nginx" in playbook
        assert "true" in playbook.lower() or "True" in playbook

    def test_render_restart_service_no_enabled(self):
        intent = Intent(
            action=ActionType.restart_service,
            target=NodeSelector(node_id="vm1"),
            params={"service": "sshd"},
            reason="test",
        )
        playbook = render_playbook(intent)
        assert "sshd" in playbook
        assert "enabled" not in playbook


class TestNodes:
    def _make_conn(self, tmp_path):
        from orchestrator.db import connect

        db_path = tmp_path / "test.db"
        conn = connect(str(db_path))
        from orchestrator.db import init_db

        init_db(conn)
        return conn

    def test_create_and_transition_task(self, tmp_path):
        conn = self._make_conn(tmp_path)
        from orchestrator.nodes import init_nodes
        init_nodes(conn, {"lxc1", "lxc2"}, api_key="")

        from orchestrator.nodes import plan_node
        from orchestrator.models import AgentState

        state = AgentState(
            user_input="test request",
            topology_context="",
            freeform_mode=False,
            intent=None, task_id=None, task_target="",
            playbook_path=None, policy_ok=False, syntax_ok=False,
            plan=None, plan_id=None, current_step_index=0,
            ansible_stdout="", ansible_stderr="", error=None, status="",
        )
        state = plan_node(state)
        task_id = state.get("task_id")
        assert task_id is not None and task_id > 0

        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        assert row["status"] == TaskStatus.planning.value

    def test_validate_valid_intent(self, tmp_path):
        conn = self._make_conn(tmp_path)
        from orchestrator.nodes import init_nodes, validate_node

        conn.execute("INSERT INTO nodes (node_id, hostname, ip, node_type, role, enabled) VALUES ('lxc1','lxc1','10.0.0.11','lxc','worker',1)")
        conn.commit()

        init_nodes(conn, {"lxc1"}, api_key="")
        state = {
            "intent": Intent(
                action=ActionType.install_pkg,
                target=NodeSelector(node_id="lxc1"),
                params={"package": "htop"},
                reason="test",
            ),
            "task_id": 1,
            "task_target": "",
            "policy_ok": False,
            "error": None,
        }
        result = validate_node(state)
        assert result["policy_ok"] is True
        assert result["task_target"] == "lxc1"

    def test_validate_unknown_node_fails(self, tmp_path):
        conn = self._make_conn(tmp_path)
        from orchestrator.nodes import init_nodes, validate_node

        conn.execute("INSERT INTO nodes (node_id, hostname, ip, node_type, role, enabled) VALUES ('lxc1','lxc1','10.0.0.11','lxc','worker',1)")
        conn.commit()

        init_nodes(conn, {"lxc1"}, api_key="")
        state = {
            "intent": Intent(
                action=ActionType.install_pkg,
                target=NodeSelector(node_id="bad-node"),
                params={"package": "htop"},
                reason="test",
            ),
            "task_id": 1,
            "task_target": "",
            "policy_ok": False,
            "error": None,
        }
        result = validate_node(state)
        assert result["policy_ok"] is False

    def test_syntax_check_nonexistent_playbook(self):
        from pathlib import Path
        from orchestrator.executor import syntax_check

        ok, stdout, stderr = syntax_check(Path("/nonexistent/playbook.yml"))
        assert not ok

    def test_graphs_compile(self):
        from langgraph.checkpoint.memory import MemorySaver
        from orchestrator.graph import create_graphs
        from orchestrator import nodes
        from orchestrator.db import connect, init_db

        conn = connect(":memory:")
        init_db(conn)
        for nid, ip in [("lxc1","10.0.0.11"),("lxc2","10.0.0.12")]:
            conn.execute("INSERT INTO nodes (node_id, hostname, ip, node_type, role, enabled) VALUES (?,?,?,?,?,1)",
                         (nid, nid, ip, "lxc", "worker"))
        conn.commit()

        nodes.init_nodes(conn, {"lxc1", "lxc2"}, api_key="")
        checkpointer = MemorySaver()
        template_graph, freeform_graph = create_graphs(checkpointer, nodes)

        assert template_graph.name == "template_graph"
        assert freeform_graph.name == "freeform_graph"
        assert "plan" in template_graph.nodes
        assert "plan_workflow" in freeform_graph.nodes

    def test_graph_runs_to_interrupt(self, tmp_path):
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.types import Command
        from orchestrator.graph import create_graphs
        from orchestrator import nodes
        from orchestrator.db import connect, init_db

        db_path = tmp_path / "test.db"
        conn = connect(str(db_path))
        init_db(conn)
        for nid, ip in [("lxc1","10.0.0.11"),("lxc2","10.0.0.12")]:
            conn.execute("INSERT INTO nodes (node_id, hostname, ip, node_type, role, enabled) VALUES (?,?,?,?,?,1)",
                         (nid, nid, ip, "lxc", "worker"))
        conn.commit()

        nodes.init_nodes(conn, {"lxc1", "lxc2"}, api_key="")
        checkpointer = MemorySaver()
        template_graph, _ = create_graphs(checkpointer, nodes)

        state = {
            "user_input": "install htop on lxc1",
            "topology_context": "## Cluster\n- lxc1",
            "freeform_mode": False,
            "intent": None, "task_id": None, "task_target": "",
            "playbook_path": None, "policy_ok": False, "syntax_ok": False,
            "plan": None, "plan_id": None, "current_step_index": 0,
            "ansible_stdout": "", "ansible_stderr": "", "error": None, "status": "",
        }

        config = {"configurable": {"thread_id": "test-graph-1"}}
        interrupted = False
        for event in template_graph.stream(state, config):
            if "__interrupt__" in event:
                interrupted = True

        assert interrupted, "Graph should interrupt at execute_node"

        for event in template_graph.stream(Command(resume="reject"), config):
            pass

        final = template_graph.get_state(config)
        assert final.values["status"] == "rejected"


class TestPlanner:
    def test_fallback_plan(self):
        from orchestrator.nodes import plan_node

        state = {
            "user_input": "install htop on lxc1",
            "topology_context": "## Cluster Topology\n- lxc1",
            "freeform_mode": False,
            "intent": None, "task_id": None, "task_target": "",
            "playbook_path": None, "policy_ok": False, "syntax_ok": False,
            "plan": None, "plan_id": None, "current_step_index": 0,
            "ansible_stdout": "", "ansible_stderr": "", "error": None, "status": "",
        }
        result = plan_node(state)
        intent = result.get("intent")
        assert intent is not None
        assert intent.action == ActionType.update_system
        assert intent.target.group == "managed_nodes"

    def test_topology_context(self):
        from orchestrator.prompts import build_topology_context

        nodes = [
            {"node_id": "lxc1", "ip": "10.0.0.11", "node_type": "lxc", "role": "worker"},
        ]
        telemetry = [
            {"node_id": "lxc1", "cpu_percent": 25.0, "ram_percent": 50.0, "disk_percent": 30.0, "uptime_seconds": 3600, "timestamp": "2026-01-01T00:00:00+00:00"},
        ]
        ctx = build_topology_context(nodes, telemetry)
        assert "lxc1" in ctx
        assert "10.0.0.11" in ctx
        assert "25.0%" in ctx
