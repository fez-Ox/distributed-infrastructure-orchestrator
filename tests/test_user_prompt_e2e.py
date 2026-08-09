from __future__ import annotations

from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from orchestrator.db import connect, init_db


class TestUserPromptEndToEnd:
    """Emulates a user typing a complex natural-language prompt into the
    orchestrator REPL and drives the whole pipeline to a successful finish."""

    def _setup(self, tmp_path, name: str):
        conn = connect(str(tmp_path / name))
        init_db(conn)
        for nid, ip in [("lxc1", "10.0.0.11"), ("lxc2", "10.0.0.12")]:
            conn.execute(
                "INSERT INTO nodes (node_id, hostname, ip, node_type, role, enabled) VALUES (?,?,?,?,?,1)",
                (nid, nid, ip, "lxc", "worker"),
            )
        conn.commit()
        return conn

    def _base_state(self, user_input: str, topology: str) -> dict:
        return {
            "user_input": user_input,
            "topology_context": topology,
            "freeform_mode": False,
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
        }

    def test_complex_prompt_template_graph_succeeds(self, tmp_path, monkeypatch):
        from orchestrator import nodes
        from orchestrator.graph import create_graphs
        from orchestrator.models import ActionType, Intent, NodeSelector

        conn = self._setup(tmp_path, "tpl.db")
        nodes.init_nodes(conn, {"lxc1", "lxc2"}, api_key="", skip_preflight=True)
        monkeypatch.setattr(nodes, "PLAYBOOK_DIR", tmp_path / "playbooks")

        class FakeTemplateLLM:
            def invoke(self, messages):
                return Intent(
                    action=ActionType.install_pkg,
                    target=NodeSelector(node_id="lxc1"),
                    params={"package": "htop"},
                    reason="Complex prompt parsed by planner",
                )

        monkeypatch.setattr(nodes, "_template_llm", FakeTemplateLLM())
        monkeypatch.setattr(nodes, "syntax_check", lambda playbook_path, local=False: (True, "ok", ""))
        monkeypatch.setattr(nodes, "ansible_run", lambda playbook_path, local=False: (True, "changed=1\nok=2\n", ""))
        monkeypatch.setattr("orchestrator.snapshot._run_playbook", lambda content, path: {"ok": True, "stdout": "MAO_SIZE=123\n", "stderr": ""})

        template_graph, _ = create_graphs(MemorySaver(), nodes)

        prompt = (
            "Please update lxc1 and lxc2 to the latest packages, install htop on lxc1, "
            "and make sure the nginx service is running before we continue."
        )
        config = {"configurable": {"thread_id": "user-prompt-tpl"}}
        state = self._base_state(prompt, "## Cluster\n- lxc1 (10.0.0.11)\n- lxc2 (10.0.0.12)")

        interrupted = False
        for event in template_graph.stream(state, config):
            if "__interrupt__" in event:
                interrupted = True

        pre = template_graph.get_state(config)
        task_id = pre.values["task_id"]
        assert interrupted, "Graph should pause for approval"
        assert pre.values["policy_ok"] is True
        assert pre.values["syntax_ok"] is True
        assert pre.values["playbook_path"] is not None
        assert Path(pre.values["playbook_path"]).exists()
        assert conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()["status"] == "awaiting_approval"

        for _ in template_graph.stream(Command(resume="approve"), config):
            pass

        final = template_graph.get_state(config)
        assert final.values["status"] == "succeeded"
        assert conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()["status"] == "succeeded"
        step = conn.execute("SELECT status, ansible_stdout FROM task_steps WHERE task_id = ?", (task_id,)).fetchone()
        assert step["status"] == "succeeded"
        assert "changed=1" in step["ansible_stdout"]

    def test_complex_prompt_freeform_workflow_succeeds(self, tmp_path, monkeypatch):
        from orchestrator import nodes
        from orchestrator.graph import create_graphs
        from orchestrator.models import Plan, WorkflowStep

        conn = self._setup(tmp_path, "ff.db")
        nodes.init_nodes(conn, {"lxc1", "lxc2"}, api_key="", skip_preflight=True)
        monkeypatch.setattr(nodes, "PLAYBOOK_DIR", tmp_path / "playbooks")

        class FakeWorkflowLLM:
            def invoke(self, messages):
                return Plan(
                    description="Provision two workers",
                    steps=[
                        WorkflowStep(
                            description="Update all packages on lxc1",
                            playbook=(
                                "---\n- hosts: lxc1\n  gather_facts: no\n  tasks:\n"
                                "    - name: Update all packages\n      ansible.builtin.package:\n"
                                "        name: '*'\n        state: latest\n"
                            ),
                        ),
                        WorkflowStep(
                            description="Add devops user on lxc2",
                            playbook=(
                                "---\n- hosts: lxc2\n  gather_facts: no\n  tasks:\n"
                                "    - name: Add devops user\n      ansible.builtin.user:\n"
                                "        name: devops\n        state: present\n"
                            ),
                        ),
                        WorkflowStep(
                            description="Restart nginx on lxc1",
                            playbook=(
                                "---\n- hosts: lxc1\n  gather_facts: no\n  tasks:\n"
                                "    - name: Restart nginx\n      ansible.builtin.service:\n"
                                "        name: nginx\n        state: restarted\n"
                            ),
                        ),
                    ],
                )

        monkeypatch.setattr(nodes, "_workflow_llm", FakeWorkflowLLM())
        monkeypatch.setattr(nodes, "ansible_run", lambda playbook_path, local=False: (True, "changed=1\nok=1\n", ""))
        monkeypatch.setattr("orchestrator.snapshot._run_playbook", lambda content, path: {"ok": True, "stdout": "MAO_SIZE=123\n", "stderr": ""})

        _, freeform_graph = create_graphs(MemorySaver(), nodes)

        prompt = (
            "Provision two workers for us: first update all packages on lxc1, then add the devops user "
            "to lxc2, and finally restart the nginx service on lxc1 so the new config takes effect."
        )
        config = {"configurable": {"thread_id": "user-prompt-ff"}}
        state = self._base_state(prompt, "## Cluster\n- lxc1 (10.0.0.11)\n- lxc2 (10.0.0.12)")

        interrupted = False
        for event in freeform_graph.stream(state, config):
            if "__interrupt__" in event:
                interrupted = True

        pre = freeform_graph.get_state(config)
        task_id = pre.values["task_id"]
        plan = pre.values["plan"]
        assert interrupted, "Graph should pause for workflow approval"
        assert plan is not None and len(plan.steps) == 3
        assert conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()["status"] == "planning"

        for _ in freeform_graph.stream(Command(resume="approve"), config):
            pass

        final = freeform_graph.get_state(config)
        assert final.values["status"] == "completed"
        assert conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()["status"] == "succeeded"
        steps = conn.execute("SELECT status FROM task_steps WHERE task_id = ? ORDER BY step_index", (task_id,)).fetchall()
        assert [s["status"] for s in steps] == ["succeeded", "succeeded", "succeeded"]
        playbooks = list((tmp_path / "playbooks").glob(f"task-{task_id}-step-*.yml"))
        assert len(playbooks) == 3
