from __future__ import annotations

import pytest

from orchestrator.policy_gate import PolicyGate, PolicyVerdict

INSTALL_HTOP = """\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Install htop
      ansible.builtin.package:
        name: htop
        state: present
"""

TEN_NODE_PLAYBOOK = """\
---
- hosts: managed_nodes
  gather_facts: false
  tasks:
    - name: Install htop
      ansible.builtin.package:
        name: htop
        state: present
"""

ELEVEN_NODES = [f"node{i}" for i in range(11)]

RM_RF_PLAYBOOK = """\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Dangerous
      ansible.builtin.shell:
        cmd: rm -rf /
"""

DEV_SDA_PLAYBOOK = """\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Write to disk
      ansible.builtin.shell:
        cmd: echo test > /dev/sda1
"""

CHMOD_777_ETC = """\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Bad permissions
      ansible.builtin.shell:
        cmd: chmod 777 /etc/passwd
"""

ORCHESTRATOR_PLAYBOOK = """\
---
- hosts: orchestrator
  gather_facts: false
  tasks:
    - name: Install htop
      ansible.builtin.package:
        name: htop
        state: present
"""

LONG_PACKAGE_NAME = "a" * 300

LONG_PKG_PLAYBOOK = f"""\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Install long
      ansible.builtin.package:
        name: {LONG_PACKAGE_NAME}
        state: present
"""

NULL_BYTES_PLAYBOOK = """\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Bad content
      ansible.builtin.copy:
        dest: /etc/test
        content: "bad\\x00stuff"
        mode: "0644"
"""

DOCKER_PLAYBOOK = """\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Run docker
      ansible.builtin.docker_container:
        name: test
        image: nginx
"""


class TestCriticalNodeRules:
    def test_critical_node_without_approval_denied(self):
        gate = PolicyGate()
        verdict = gate.evaluate_playbook(ORCHESTRATOR_PLAYBOOK, ["vm1"], {})
        assert not verdict.allowed
        assert "orchestrator" in verdict.reason.lower()

    def test_critical_node_with_approval_allowed(self):
        gate = PolicyGate()
        verdict = gate.evaluate_playbook(ORCHESTRATOR_PLAYBOOK, ["vm1"], {"human_approval": True})
        assert verdict.allowed

    def test_non_critical_node_passes(self):
        gate = PolicyGate()
        verdict = gate.evaluate_playbook(INSTALL_HTOP, ["lxc1"], {})
        assert verdict.allowed


class TestBlastRadius:
    def test_single_node_task_passes(self):
        gate = PolicyGate(max_nodes=10)
        verdict = gate.evaluate_playbook(INSTALL_HTOP, ["lxc1"], {})
        assert verdict.allowed

    def test_multi_node_task_under_limit_passes(self):
        gate = PolicyGate(max_nodes=10)
        verdict = gate.evaluate_playbook(TEN_NODE_PLAYBOOK, list(range(5)), {})
        assert verdict.allowed

    def test_task_exceeding_max_nodes_denied(self):
        gate = PolicyGate(max_nodes=10)
        verdict = gate.evaluate_playbook(TEN_NODE_PLAYBOOK, ELEVEN_NODES, {})
        assert not verdict.allowed
        assert "blast radius" in verdict.reason.lower()
        assert verdict.escalate_to_human


class TestDangerousPatterns:
    def test_rm_rf_in_playbook_denied(self):
        gate = PolicyGate()
        verdict = gate.evaluate_playbook(RM_RF_PLAYBOOK, ["lxc1"], {})
        assert not verdict.allowed
        assert "dangerous" in verdict.reason.lower()

    def test_dev_sda_write_denied(self):
        gate = PolicyGate()
        verdict = gate.evaluate_playbook(DEV_SDA_PLAYBOOK, ["lxc1"], {})
        assert not verdict.allowed

    def test_chmod_777_etc_denied(self):
        gate = PolicyGate()
        verdict = gate.evaluate_playbook(CHMOD_777_ETC, ["lxc1"], {})
        assert not verdict.allowed


class TestResourceBounds:
    def test_excessively_long_package_name_denied(self):
        gate = PolicyGate()
        verdict = gate.evaluate_playbook(LONG_PKG_PLAYBOOK, ["lxc1"], {})
        assert not verdict.allowed
        assert "exceeds" in verdict.reason.lower()

    def test_null_bytes_in_playbook_denied(self):
        gate = PolicyGate()
        verdict = gate.evaluate_playbook(NULL_BYTES_PLAYBOOK, ["lxc1"], {})
        assert not verdict.allowed
        assert "null" in verdict.reason.lower()


class TestCombined:
    def test_all_rules_evaluated_independently(self):
        gate = PolicyGate()
        verdict = gate.evaluate_playbook(RM_RF_PLAYBOOK, ["lxc1"], {})
        assert not verdict.allowed
        assert "dangerous" in verdict.reason.lower()

    def test_first_deny_stops_evaluation(self):
        gate = PolicyGate(max_nodes=1)
        verdict = gate.evaluate_playbook(RM_RF_PLAYBOOK, ELEVEN_NODES, {})
        assert not verdict.allowed
        assert "blast radius" in verdict.reason.lower()

    def test_verdict_stored_on_task_record(self):
        gate = PolicyGate()
        verdict = gate.evaluate_playbook(INSTALL_HTOP, ["lxc1"], {})
        assert verdict.allowed
        assert verdict.reason == ""

    def test_escalation_requires_human_review(self):
        gate = PolicyGate()
        verdict = gate.evaluate_playbook(ORCHESTRATOR_PLAYBOOK, ["vm1"], {})
        assert not verdict.allowed
        assert verdict.escalate_to_human
