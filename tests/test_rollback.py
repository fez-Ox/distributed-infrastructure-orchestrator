from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from orchestrator.db import connect, init_db
from orchestrator import nodes
from orchestrator.models import ActionType, NodeSelector, Plan, TaskStatus, WorkflowStep
from orchestrator.nodes import rollback_workflow_task


class TestPlanRollback:
    """Comprehensive test suite for Phase 6: Plan Rollback functionality."""

    def _setup_db(self, tmp_path, db_name="rollback_test.db") -> sqlite3.Connection:
        db_path = tmp_path / db_name
        conn = connect(str(db_path))
        init_db(conn)
        for nid, ip in [("lxc1", "10.0.0.11"), ("lxc2", "10.0.0.12")]:
            conn.execute(
                "INSERT INTO nodes (node_id, hostname, ip, node_type, role, enabled) VALUES (?, ?, ?, ?, ?, 1)",
                (nid, nid, ip, "lxc", "worker"),
            )
        conn.commit()
        return conn

    def test_workflow_step_rollback_model(self):
        """Verifies that WorkflowStep parses and serializes rollback_playbook field."""
        step = WorkflowStep(
            description="Install htop",
            playbook="---\n- hosts: lxc1\n  tasks:\n    - ansible.builtin.package: name=htop state=present\n",
            rollback_playbook="---\n- hosts: lxc1\n  tasks:\n    - ansible.builtin.package: name=htop state=absent\n",
            verification="Check htop installed",
        )
        data = step.model_dump()
        assert data["rollback_playbook"] is not None
        assert "state=absent" in data["rollback_playbook"]

        reconstructed = WorkflowStep.model_validate(data)
        assert reconstructed.rollback_playbook == step.rollback_playbook

    def test_db_migration_rollback_columns(self, tmp_path):
        """Verifies that init_db creates/migrates task_steps with rollback columns."""
        conn = self._setup_db(tmp_path, "migration.db")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_steps)").fetchall()}
        assert "rollback_playbook_yaml" in cols
        assert "rollback_status" in cols
        assert "rollback_ansible_stdout" in cols
        assert "rollback_ansible_stderr" in cols

    def test_rollback_workflow_task_success(self, tmp_path, monkeypatch):
        """Tests successful reverse rollback execution across completed workflow steps."""
        conn = self._setup_db(tmp_path, "success.db")
        nodes.init_nodes(conn, {"lxc1", "lxc2"}, api_key="", skip_preflight=True)
        monkeypatch.setattr(nodes, "PLAYBOOK_DIR", tmp_path / "playbooks")

        # Create a task with 2 succeeded steps
        cursor = conn.execute(
            "INSERT INTO tasks (user_request, task_type, status) VALUES (?, ?, ?)",
            ("Install tools", "freeform", "failed"),
        )
        task_id = cursor.lastrowid

        conn.execute(
            "INSERT INTO task_steps (task_id, step_index, description, playbook_yaml, rollback_playbook_yaml, status) VALUES (?, ?, ?, ?, ?, ?)",
            (
                task_id,
                0,
                "Install htop on lxc1",
                "---\n- hosts: lxc1\n  gather_facts: no\n  tasks:\n    - name: Install htop\n      ansible.builtin.package:\n        name: htop\n        state: present\n",
                "---\n- hosts: lxc1\n  gather_facts: no\n  tasks:\n    - name: Remove htop\n      ansible.builtin.package:\n        name: htop\n        state: absent\n",
                "succeeded",
            ),
        )
        conn.execute(
            "INSERT INTO task_steps (task_id, step_index, description, playbook_yaml, rollback_playbook_yaml, status) VALUES (?, ?, ?, ?, ?, ?)",
            (
                task_id,
                1,
                "Add user webmaster on lxc1",
                "---\n- hosts: lxc1\n  gather_facts: no\n  tasks:\n    - name: Add user webmaster\n      ansible.builtin.user:\n        name: webmaster\n        state: present\n",
                "---\n- hosts: lxc1\n  gather_facts: no\n  tasks:\n    - name: Remove user webmaster\n      ansible.builtin.user:\n        name: webmaster\n        state: absent\n",
                "succeeded",
            ),
        )
        conn.commit()

        executed_playbooks = []

        def mock_syntax(path):
            return (True, "syntax ok", "")

        def mock_run(path):
            executed_playbooks.append(path.name)
            return (True, "changed=1 ok=1", "")

        monkeypatch.setattr(nodes, "syntax_check", mock_syntax)
        monkeypatch.setattr(nodes, "ansible_run", mock_run)

        ok, msg = rollback_workflow_task(conn, task_id)
        assert ok is True
        assert "successfully rolled back" in msg.lower()

        # Verify steps were executed in reverse order (step 1 rollback then step 0 rollback)
        assert len(executed_playbooks) == 2
        assert executed_playbooks[0] == f"task-{task_id}-step-1-rollback.yml"
        assert executed_playbooks[1] == f"task-{task_id}-step-0-rollback.yml"

        # Check DB status updates
        t_row = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        assert t_row["status"] == TaskStatus.rolled_back.value

        steps = conn.execute("SELECT rollback_status FROM task_steps WHERE task_id = ? ORDER BY step_index", (task_id,)).fetchall()
        assert [s["rollback_status"] for s in steps] == ["succeeded", "succeeded"]

    def test_rollback_workflow_task_validation_failure(self, tmp_path, monkeypatch):
        """Tests that malicious rollback playbooks (banned path) are rejected before execution."""
        conn = self._setup_db(tmp_path, "security.db")
        nodes.init_nodes(conn, {"lxc1", "lxc2"}, api_key="", skip_preflight=True)
        monkeypatch.setattr(nodes, "PLAYBOOK_DIR", tmp_path / "playbooks")

        cursor = conn.execute(
            "INSERT INTO tasks (user_request, task_type, status) VALUES (?, ?, ?)",
            ("Dangerous setup", "freeform", "failed"),
        )
        task_id = cursor.lastrowid

        # Insert a step with a malicious rollback playbook trying to overwrite /etc/shadow
        conn.execute(
            "INSERT INTO task_steps (task_id, step_index, description, playbook_yaml, rollback_playbook_yaml, status) VALUES (?, ?, ?, ?, ?, ?)",
            (
                task_id,
                0,
                "Safe step",
                "---\n- hosts: lxc1\n  tasks:\n    - name: Safe\n      ansible.builtin.package:\n        name: htop\n        state: present\n",
                "---\n- hosts: lxc1\n  tasks:\n    - name: Banned file copy\n      ansible.builtin.copy:\n        dest: /etc/shadow\n        content: 'hacked'\n",
                "succeeded",
            ),
        )
        conn.commit()

        ok, msg = rollback_workflow_task(conn, task_id)
        assert ok is False
        assert "validation failed" in msg.lower() or "banned path" in msg.lower()

        t_row = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        assert t_row["status"] == TaskStatus.rollback_failed.value

    def test_rollback_workflow_task_execution_failure_halts(self, tmp_path, monkeypatch):
        """Tests that when a rollback playbook execution fails, execution halts immediately."""
        conn = self._setup_db(tmp_path, "fail_halt.db")
        nodes.init_nodes(conn, {"lxc1", "lxc2"}, api_key="", skip_preflight=True)
        monkeypatch.setattr(nodes, "PLAYBOOK_DIR", tmp_path / "playbooks")

        cursor = conn.execute(
            "INSERT INTO tasks (user_request, task_type, status) VALUES (?, ?, ?)",
            ("Multi-step task", "freeform", "failed"),
        )
        task_id = cursor.lastrowid

        conn.execute(
            "INSERT INTO task_steps (task_id, step_index, description, playbook_yaml, rollback_playbook_yaml, status) VALUES (?, ?, ?, ?, ?, ?)",
            (
                task_id,
                0,
                "Step 1",
                "---\n- hosts: lxc1\n  tasks:\n    - ansible.builtin.package: name=htop state=present\n",
                "---\n- hosts: lxc1\n  tasks:\n    - ansible.builtin.package: name=htop state=absent\n",
                "succeeded",
            ),
        )
        conn.execute(
            "INSERT INTO task_steps (task_id, step_index, description, playbook_yaml, rollback_playbook_yaml, status) VALUES (?, ?, ?, ?, ?, ?)",
            (
                task_id,
                1,
                "Step 2",
                "---\n- hosts: lxc1\n  tasks:\n    - ansible.builtin.user: name=devops state=present\n",
                "---\n- hosts: lxc1\n  tasks:\n    - ansible.builtin.user: name=devops state=absent\n",
                "succeeded",
            ),
        )
        conn.commit()

        runs = []

        def mock_syntax(path):
            return (True, "syntax ok", "")

        def mock_run(path):
            runs.append(path.name)
            if "step-1" in path.name:
                return (False, "", "SSH Connection refused")
            return (True, "ok", "")

        monkeypatch.setattr(nodes, "syntax_check", mock_syntax)
        monkeypatch.setattr(nodes, "ansible_run", mock_run)

        ok, msg = rollback_workflow_task(conn, task_id)
        assert ok is False
        assert "execution failed" in msg.lower() or "connection refused" in msg.lower()

        # Step 1 rollback failed, so step 0 rollback should never have been run
        assert runs == [f"task-{task_id}-step-1-rollback.yml"]

        t_row = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        assert t_row["status"] == TaskStatus.rollback_failed.value
