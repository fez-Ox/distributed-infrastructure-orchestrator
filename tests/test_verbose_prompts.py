from __future__ import annotations

import os
import pytest
from pathlib import Path
from dotenv import load_dotenv

from orchestrator.db import connect, init_db
from orchestrator import nodes
from orchestrator.models import ActionType, Plan, Intent
from orchestrator.playbook_validator import validate_playbook
from orchestrator.policy_gate import PolicyGate
from orchestrator.prompts import build_topology_context

# Ensure environment variables are loaded
load_dotenv()

HAS_GEMINI_KEY = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY"))


class TestVerbosePrompts:
    """Comprehensive test suite evaluating system handling of long, verbose prompts."""

    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        db_path = tmp_path / "verbose_test.db"
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
        nodes.init_nodes(conn, {"vm1", "vm2", "lxc1", "lxc2"}, api_key=os.environ.get("GEMINI_API_KEY", ""), skip_preflight=True)
        yield
        conn.close()

    def _topology_string(self) -> str:
        from orchestrator.agent import fetch_nodes, fetch_telemetry
        all_nodes = fetch_nodes(self.conn)
        telemetry = fetch_telemetry(self.conn)
        return build_topology_context(all_nodes, telemetry)

    @pytest.mark.skipif(not HAS_GEMINI_KEY, reason="Requires GEMINI_API_KEY or OPENAI_API_KEY in .env")
    def test_verbose_multi_step_workflow_llm(self):
        """Evaluates how the LLM decomposes a long, multi-paragraph setup request into steps."""
        prompt = (
            "We are setting up a multi-node web service across our Proxmox cluster. "
            "Please perform the following operations in order:\n"
            "1. First, make sure all LXC nodes (lxcs) have their system packages updated to the latest version.\n"
            "2. Second, install nginx on lxc1 so it can act as our front-end web server.\n"
            "3. Third, create a new system user named 'webmaster' on lxc1 with bash shell and add them to the sudo group.\n"
            "4. Fourth, create a custom configuration state file at '/opt/mao/state/site_config.json' with content "
            "'{\"environment\": \"production\", \"app\": \"frontend\"}'.\n"
            "5. Finally, ensure the nginx service is started and enabled on lxc1."
        )

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

        assert plan is not None, f"Workflow planner should return a Plan object. Error: {res_state.get('error')}"
        assert isinstance(plan, Plan), f"Expected Plan, got {type(plan)}"
        assert len(plan.steps) >= 3, f"Expected multi-step plan (at least 3 steps), got {len(plan.steps)} steps"
        assert plan.description != "", "Plan should have a valid summary description"

        gate = PolicyGate()
        for idx, step in enumerate(plan.steps):
            assert step.description != "", f"Step {idx} should have a description"
            assert step.playbook != "", f"Step {idx} should have playbook YAML"
            assert step.playbook.startswith("---"), f"Step {idx} playbook must start with ---"

            # Check playbook validator and policy gate on each step's playbook
            val_errors = validate_playbook(step.playbook)
            assert len(val_errors) == 0, f"Step {idx} playbook validation failed: {val_errors}"

            verdict = gate.evaluate_playbook(step.playbook, target_nodes=["lxc1", "lxc2"])
            assert verdict.allowed, f"Step {idx} policy gate failed: {verdict.reason}"

    @pytest.mark.skipif(not HAS_GEMINI_KEY, reason="Requires GEMINI_API_KEY or OPENAI_API_KEY in .env")
    def test_verbose_template_mode_intent_llm(self):
        """Evaluates structured intent planning for a long prompt in Template mode."""
        prompt = (
            "Hey orchestrator, I noticed that our worker nodes need utility tools installed. "
            "Could you please install the package 'htop' across all of our LXC worker nodes "
            "so we can monitor system resource usage across the container cluster?"
        )

        topo = self._topology_string()
        state = {
            "user_input": prompt,
            "topology_context": topo,
            "freeform_mode": False,
            "intent": None,
            "task_id": None,
            "plan": None,
            "current_step_index": 0,
            "ansible_stdout": "",
            "ansible_stderr": "",
            "error": None,
            "status": "",
        }
        res_state = nodes.plan_node(state)
        intent: Intent = res_state.get("intent")

        assert intent is not None, "Intent planner should return a valid Intent"
        assert intent.action in (ActionType.install_pkg, ActionType.update_system), f"Unexpected action: {intent.action}"

        # Target should resolve to group or node_id
        assert intent.target.group in ("lxcs", "managed_nodes") or intent.target.node_id in ("lxc1", "lxc2")

    def test_verbose_prompt_with_embedded_security_attack(self):
        """Tests that long, verbose prompts with embedded dangerous actions are caught by guardrails."""
        malicious_playbook = (
            "---\n"
            "- hosts: lxc1\n"
            "  gather_facts: no\n"
            "  tasks:\n"
            "    - name: Perform routine system update\n"
            "      ansible.builtin.package:\n"
            "        name: htop\n"
            "        state: present\n"
            "    - name: Malicious path access attempt\n"
            "      ansible.builtin.copy:\n"
            "        dest: /etc/shadow\n"
            "        content: 'hacked'\n"
        )

        val_errors = validate_playbook(malicious_playbook)
        assert len(val_errors) > 0, "Playbook validator should reject access to /etc/shadow"
        assert any("banned path" in err.lower() or "/etc/shadow" in err for err in val_errors)

        gate = PolicyGate()
        verdict = gate.evaluate_playbook(malicious_playbook, target_nodes=["lxc1"])
        assert not verdict.allowed, "PolicyGate should reject banned path access"
        assert "shadow" in verdict.reason.lower() or "banned path" in verdict.reason.lower()

    def test_verbose_prompt_with_banned_command_injection(self):
        """Tests that verbose playbooks attempting command injection via raw shell are caught."""
        shell_playbook = (
            "---\n"
            "- hosts: lxc1\n"
            "  gather_facts: no\n"
            "  tasks:\n"
            "    - name: Run unauthorized shell script\n"
            "      ansible.builtin.shell:\n"
            "        cmd: rm -rf /\n"
        )

        val_errors = validate_playbook(shell_playbook, allow_command_modules=False)
        assert len(val_errors) > 0, "Playbook validator should reject ansible.builtin.shell module"
        assert any("disallowed module" in err.lower() or "shell" in err.lower() for err in val_errors)

        gate = PolicyGate()
        verdict = gate.evaluate_playbook(shell_playbook, target_nodes=["lxc1"])
        assert not verdict.allowed, "PolicyGate should reject unauthorized module and dangerous pattern"

    def test_verbose_child_delegation_prompt(self):
        """Tests child delegation prompt processing for long inspection queries."""
        prompt = "Please inspect lxc1: check if the nginx service is running, tail logs, and report local readiness"
        
        from orchestrator.agent import detect_child_delegate
        result = detect_child_delegate(prompt, {"lxc1", "lxc2", "vm1", "vm2"})
        assert result is not None, "detect_child_delegate should detect inspection prompt"
        node_id, req = result
        assert node_id == "lxc1"
        assert req == prompt

        intent = nodes.plan_child_intent(prompt, "lxc1")
        assert intent is not None, "Child planner should resolve inspection request to intent"
        assert intent.action in (
            ActionType.service_status,
            ActionType.workload_readiness,
            ActionType.log_tail,
            ActionType.disk_check,
            ActionType.capabilities_report,
        )
