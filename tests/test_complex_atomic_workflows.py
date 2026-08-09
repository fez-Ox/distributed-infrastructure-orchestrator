from __future__ import annotations

import os
import sqlite3
import pytest
from pathlib import Path
from dotenv import load_dotenv

from orchestrator.db import connect, init_db
from orchestrator import nodes
from orchestrator.models import Plan, TaskStatus, WorkflowStep
from orchestrator.playbook_validator import validate_playbook
from orchestrator.policy_gate import PolicyGate
from orchestrator.prompts import build_topology_context
from orchestrator.nodes import rollback_workflow_task

load_dotenv()
RUN_LIVE_LLM = os.environ.get("RUN_LIVE_LLM", "").lower() in ("1", "true", "yes")
HAS_GEMINI_KEY = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY"))


class TestComplexAtomicWorkflows:
    """Evaluates orchestrator robustness against concise prompts requiring complex, multi-step sequences of atomic actions."""

    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        db_path = tmp_path / "complex_workflow_test.db"
        conn = connect(str(db_path))
        init_db(conn)
        for nid, ip, ntype, role in [
            ("vm1", "192.168.122.88", "vm", "orchestrator"),
            ("vm2", "192.168.122.89", "vm", "worker"),
            ("lxc1", "192.168.122.238", "lxc", "worker"),
            ("lxc2", "192.168.122.239", "lxc", "worker"),
        ]:
            conn.execute(
                "INSERT INTO nodes (node_id, hostname, ip, node_type, role, enabled) VALUES (?, ?, ?, ?, ?, 1)",
                (nid, nid, ip, ntype, role),
            )
        conn.commit()
        self.conn = conn
        nodes.init_nodes(
            conn,
            {"vm1", "vm2", "lxc1", "lxc2"},
            api_key=os.environ.get("GEMINI_API_KEY", ""),
            skip_preflight=True,
        )
        yield
        conn.close()

    def _topology_string(self) -> str:
        from orchestrator.agent import fetch_nodes, fetch_telemetry
        all_nodes = fetch_nodes(self.conn)
        telemetry = fetch_telemetry(self.conn)
        return build_topology_context(all_nodes, telemetry)

    def _validate_plan_structure(self, plan: Plan, min_steps: int = 2) -> None:
        assert plan is not None, "Plan must not be None"
        assert isinstance(plan, Plan), f"Expected Plan instance, got {type(plan)}"
        assert len(plan.steps) >= min_steps, f"Expected at least {min_steps} steps, got {len(plan.steps)}"
        assert plan.description != "", "Plan description must not be empty"

        gate = PolicyGate()
        for idx, step in enumerate(plan.steps):
            assert step.description != "", f"Step {idx} description empty"
            assert step.playbook != "", f"Step {idx} playbook empty"
            assert step.playbook.startswith("---"), f"Step {idx} playbook must start with ---"

            val_errors = validate_playbook(step.playbook)
            assert len(val_errors) == 0, f"Step {idx} playbook validation failed: {val_errors}"

            verdict = gate.evaluate_playbook(step.playbook, target_nodes=["lxc1", "lxc2", "vm2"])
            assert verdict.allowed, f"Step {idx} policy gate failed: {verdict.reason}"

            assert step.rollback_playbook is not None, f"Step {idx} missing rollback_playbook"
            assert step.rollback_playbook.startswith("---"), f"Step {idx} rollback_playbook must start with ---"
            rb_val_errors = validate_playbook(step.rollback_playbook)
            assert len(rb_val_errors) == 0, f"Step {idx} rollback validation failed: {rb_val_errors}"

    def test_fast_mocked_postgres_cluster_decomposition(self, monkeypatch):
        """Fast unit test verifying multi-step atomic sequence & paired rollbacks for HA Postgres."""
        mock_plan = Plan(
            description="Deploy HA PostgreSQL cluster across lxc1 and lxc2",
            steps=[
                WorkflowStep(
                    description="Install postgresql packages on lxc1 and lxc2",
                    playbook="---\n- hosts: lxcs\n  tasks:\n    - name: Install postgresql\n      ansible.builtin.package:\n        name: postgresql\n        state: present\n",
                    rollback_playbook="---\n- hosts: lxcs\n  tasks:\n    - name: Remove postgresql\n      ansible.builtin.package:\n        name: postgresql\n        state: absent\n",
                ),
                WorkflowStep(
                    description="Create replication user postgres_repl",
                    playbook="---\n- hosts: lxcs\n  tasks:\n    - name: Create replicator user\n      ansible.builtin.user:\n        name: postgres_repl\n        state: present\n",
                    rollback_playbook="---\n- hosts: lxcs\n  tasks:\n    - name: Remove replicator user\n      ansible.builtin.user:\n        name: postgres_repl\n        state: absent\n",
                ),
                WorkflowStep(
                    description="Configure pg_hba.conf for replication on lxc1",
                    playbook="---\n- hosts: lxc1\n  tasks:\n    - name: Deploy pg_hba config\n      ansible.builtin.copy:\n        dest: /opt/mao/state/pg_hba.conf\n        content: 'host replication all 10.0.0.0/24 md5'\n",
                    rollback_playbook="---\n- hosts: lxc1\n  tasks:\n    - name: Remove pg_hba config\n      ansible.builtin.file:\n        path: /opt/mao/state/pg_hba.conf\n        state: absent\n",
                ),
                WorkflowStep(
                    description="Start and enable postgresql service",
                    playbook="---\n- hosts: lxcs\n  tasks:\n    - name: Ensure postgresql running\n      ansible.builtin.service:\n        name: postgresql\n        state: started\n",
                    rollback_playbook="---\n- hosts: lxcs\n  tasks:\n    - name: Stop postgresql\n      ansible.builtin.service:\n        name: postgresql\n        state: stopped\n",
                ),
            ],
        )

        class MockLLM:
            def invoke(self, messages):
                return mock_plan

        monkeypatch.setattr(nodes, "_workflow_llm", MockLLM())

        topo = self._topology_string()
        state = {
            "user_input": "Deploy a secure HA PostgreSQL replica cluster across lxc1 and lxc2",
            "topology_context": topo,
            "freeform_mode": True,
            "intent": None,
            "task_id": None,
            "plan": None,
            "current_step_index": 0,
            "ansible_stdout": "",
            "ansible_stderr": "",
            "error": None,
            "status": "",
        }
        res_state = nodes.plan_workflow_node(state)
        plan: Plan = res_state.get("plan")

        self._validate_plan_structure(plan, min_steps=3)

    def test_fast_mocked_soc2_hardening_decomposition(self, monkeypatch):
        """Fast unit test verifying multi-step atomic sequence for SOC2 hardening."""
        mock_plan = Plan(
            description="Harden security across all worker nodes for SOC2 compliance",
            steps=[
                WorkflowStep(
                    description="Install fail2ban and ufw security packages",
                    playbook="---\n- hosts: managed_nodes\n  tasks:\n    - name: Install fail2ban\n      ansible.builtin.package:\n        name: fail2ban\n        state: present\n",
                    rollback_playbook="---\n- hosts: managed_nodes\n  tasks:\n    - name: Remove fail2ban\n      ansible.builtin.package:\n        name: fail2ban\n        state: absent\n",
                ),
                WorkflowStep(
                    description="Create audit user sysaudit",
                    playbook="---\n- hosts: managed_nodes\n  tasks:\n    - name: Create sysaudit user\n      ansible.builtin.user:\n        name: sysaudit\n        state: present\n",
                    rollback_playbook="---\n- hosts: managed_nodes\n  tasks:\n    - name: Remove sysaudit user\n      ansible.builtin.user:\n        name: sysaudit\n        state: absent\n",
                ),
                WorkflowStep(
                    description="Deploy hardened SSH configuration",
                    playbook="---\n- hosts: managed_nodes\n  tasks:\n    - name: Deploy SSH security config\n      ansible.builtin.copy:\n        dest: /opt/mao/state/sshd_hardened.conf\n        content: 'PermitRootLogin no\\nMaxAuthTries 3'\n",
                    rollback_playbook="---\n- hosts: managed_nodes\n  tasks:\n    - name: Remove SSH security config\n      ansible.builtin.file:\n        path: /opt/mao/state/sshd_hardened.conf\n        state: absent\n",
                ),
            ],
        )

        class MockLLM:
            def invoke(self, messages):
                return mock_plan

        monkeypatch.setattr(nodes, "_workflow_llm", MockLLM())

        topo = self._topology_string()
        state = {
            "user_input": "Harden security across all worker nodes for SOC2 compliance",
            "topology_context": topo,
            "freeform_mode": True,
            "intent": None,
            "task_id": None,
            "plan": None,
            "current_step_index": 0,
            "ansible_stdout": "",
            "ansible_stderr": "",
            "error": None,
            "status": "",
        }
        res_state = nodes.plan_workflow_node(state)
        plan: Plan = res_state.get("plan")

        self._validate_plan_structure(plan, min_steps=3)

    def test_complex_plan_rollback_execution(self, tmp_path, monkeypatch):
        """Validates that a complex multi-step plan rolls back in reverse order when requested."""
        monkeypatch.setattr(nodes, "PLAYBOOK_DIR", tmp_path / "playbooks")

        cursor = self.conn.execute(
            "INSERT INTO tasks (user_request, task_type, status) VALUES (?, ?, ?)",
            ("Deploy HA Postgres", "freeform", "failed"),
        )
        task_id = cursor.lastrowid

        steps_data = [
            ("Install postgresql", "---\n- hosts: lxcs\n  tasks:\n    - ansible.builtin.package: name=postgresql state=present\n", "---\n- hosts: lxcs\n  tasks:\n    - ansible.builtin.package: name=postgresql state=absent\n"),
            ("Create postgres user", "---\n- hosts: lxcs\n  tasks:\n    - ansible.builtin.user: name=postgres_repl state=present\n", "---\n- hosts: lxcs\n  tasks:\n    - ansible.builtin.user: name=postgres_repl state=absent\n"),
            ("Deploy config", "---\n- hosts: lxcs\n  tasks:\n    - ansible.builtin.copy: dest=/opt/mao/state/pg.conf content='conf'\n", "---\n- hosts: lxcs\n  tasks:\n    - ansible.builtin.file: path=/opt/mao/state/pg.conf state=absent\n"),
        ]

        for i, (desc, pb, rb) in enumerate(steps_data):
            self.conn.execute(
                "INSERT INTO task_steps (task_id, step_index, description, playbook_yaml, rollback_playbook_yaml, status) VALUES (?, ?, ?, ?, ?, 'succeeded')",
                (task_id, i, desc, pb, rb),
            )
        self.conn.commit()

        executed = []

        def mock_syntax(path):
            return (True, "syntax ok", "")

        def mock_run(path):
            executed.append(path.name)
            return (True, "changed=1 ok=1", "")

        monkeypatch.setattr(nodes, "syntax_check", mock_syntax)
        monkeypatch.setattr(nodes, "ansible_run", mock_run)

        ok, msg = rollback_workflow_task(self.conn, task_id)
        assert ok is True
        assert len(executed) == 3
        assert executed[0] == f"task-{task_id}-step-2-rollback.yml"
        assert executed[1] == f"task-{task_id}-step-1-rollback.yml"
        assert executed[2] == f"task-{task_id}-step-0-rollback.yml"

        t_row = self.conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        assert t_row["status"] == TaskStatus.rolled_back.value

    @pytest.mark.skipif(not (RUN_LIVE_LLM and HAS_GEMINI_KEY), reason="Live LLM tests skipped by default to ensure fast test execution. Set RUN_LIVE_LLM=1 to run.")
    def test_live_llm_complex_postgres_cluster(self):
        """Live LLM evaluation: HA PostgreSQL replica cluster deployment."""
        prompt = "Deploy a secure HA PostgreSQL replica cluster across lxc1 and lxc2"
        topo = self._topology_string()
        state = {
            "user_input": prompt,
            "topology_context": topo,
            "freeform_mode": True,
            "intent": None,
            "task_id": None,
            "plan": None,
            "current_step_index": 0,
            "ansible_stdout": "",
            "ansible_stderr": "",
            "error": None,
            "status": "",
        }
        res_state = nodes.plan_workflow_node(state)
        plan: Plan = res_state.get("plan")
        self._validate_plan_structure(plan, min_steps=3)
