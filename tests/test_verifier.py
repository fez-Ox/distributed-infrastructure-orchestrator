from __future__ import annotations

import pytest

from orchestrator.primitives import PrimitiveCall
from orchestrator.verifier import (
    VerifierResult,
    format_primitives_for_verifier,
    verify_primitive_sequence,
)


class TestVerifierNoLLM:
    def test_match_when_no_llm_configured(self):
        calls = [PrimitiveCall(action="install_package", params={"package": "htop", "state": "present"})]
        result, reason = verify_primitive_sequence("install htop", calls, "## Cluster\n- lxc1", llm=None)
        assert result == VerifierResult.MATCH
        assert "No verifier LLM" in reason


class TestVerifierResponseParsing:
    def test_parse_match_response(self):
        class MockResponse:
            content = "MATCH\nAll actions match the request."

        class MockLLM:
            def invoke(self, messages):
                return MockResponse()

        calls = [PrimitiveCall(action="install_package", params={"package": "htop", "state": "present"})]
        result, reason = verify_primitive_sequence("install htop", calls, "", llm=MockLLM())
        assert result == VerifierResult.MATCH
        assert "All actions match" in reason

    def test_parse_scope_mismatch(self):
        class MockResponse:
            content = "SCOPE_MISMATCH\nTargets 10 nodes but request mentioned only lxc1"

        class MockLLM:
            def invoke(self, messages):
                return MockResponse()

        calls = [PrimitiveCall(action="restart_service", params={"service": "nginx", "state": "restarted"})]
        result, reason = verify_primitive_sequence("restart nginx on lxc1", calls, "", llm=MockLLM())
        assert result == VerifierResult.SCOPE_MISMATCH
        assert "Targets 10 nodes" in reason

    def test_parse_ambiguous(self):
        class MockResponse:
            content = "AMBIGUOUS\nRequest is too vague"

        class MockLLM:
            def invoke(self, messages):
                return MockResponse()

        calls = [PrimitiveCall(action="update_system", params={})]
        result, reason = verify_primitive_sequence("fix the server", calls, "", llm=MockLLM())
        assert result == VerifierResult.AMBIGUOUS
        assert "vague" in reason

    def test_no_shared_context_in_prompt(self):
        class MockLLM:
            def invoke(self, messages):
                sys_msg = messages[0].content
                human_msg = messages[1].content
                # Check no planner reasoning is shared
                assert "planner" not in sys_msg.lower() or "safety verifier" in sys_msg
                assert "primitive" in human_msg.lower()
                assert "install htop" in human_msg

                class MockResponse:
                    content = "MATCH"

                return MockResponse()

        calls = [PrimitiveCall(action="install_package", params={"package": "htop", "state": "present"})]
        verify_primitive_sequence("install htop on lxc1", calls, "## Cluster\n- lxc1", llm=MockLLM())

    def test_llm_error_falls_back_to_ambiguous(self):
        class MockLLM:
            def invoke(self, messages):
                raise RuntimeError("API error")

        calls = [PrimitiveCall(action="install_package", params={"package": "htop", "state": "present"})]
        result, reason = verify_primitive_sequence("install htop", calls, "", llm=MockLLM())
        assert result == VerifierResult.AMBIGUOUS
        assert "LLM error" in reason


class TestVerifierIntegration:
    def test_verifier_logged_on_task(self, tmp_path):
        from orchestrator.db import connect, init_db

        db_path = tmp_path / "test.db"
        conn = connect(str(db_path))
        init_db(conn)
        conn.execute(
            "INSERT INTO tasks (user_request, task_type, target_node_id, status) VALUES (?, ?, ?, ?)",
            ("install htop", "template", "lxc1", "planning"),
        )
        conn.commit()
        row = conn.execute("SELECT task_id FROM tasks ORDER BY task_id DESC LIMIT 1").fetchone()
        task_id = row[0]

        from orchestrator.primitives import PrimitiveCall
        from orchestrator.verifier import VerifierResult

        calls = [PrimitiveCall(action="install_package", params={"package": "htop", "state": "present"})]
        result, reason = verify_primitive_sequence("install htop", calls, "", llm=None)

        conn.execute(
            "UPDATE tasks SET verifier_result = ?, verifier_reason = ? WHERE task_id = ?",
            (result.value, reason, task_id),
        )
        conn.commit()

        updated = conn.execute("SELECT verifier_result, verifier_reason FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        assert updated["verifier_result"] == "MATCH"
        assert updated["verifier_reason"] is not None

    def test_verifier_does_not_retry_silently(self):
        state = {"count": 0}

        class FailingLLM:
            def invoke(self, messages):
                state["count"] += 1
                class R:
                    content = "SCOPE_MISMATCH\nToo many nodes" if state["count"] == 1 else "MATCH"
                return R()

        calls = [PrimitiveCall(action="install_package", params={"package": "htop", "state": "present"})]
        result, _ = verify_primitive_sequence("install htop on lxc1", calls, "", llm=FailingLLM())
        assert state["count"] == 1
        assert result == VerifierResult.SCOPE_MISMATCH
    def test_format_primitives(self):
        calls = [
            PrimitiveCall(action="install_package", params={"package": "nginx"}),
            PrimitiveCall(action="ensure_service_state", params={"service": "nginx", "state": "running"}),
        ]
        formatted = format_primitives_for_verifier(calls)
        assert "install_package" in formatted
        assert "ensure_service_state" in formatted
        assert "nginx" in formatted
        assert "running" in formatted
