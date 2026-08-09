from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from orchestrator.child_protocol import (
    CapabilityReport,
    Heartbeat,
    ReadinessRequest,
    TaskAck,
    TaskRequest,
    TaskResult,
    decode_message,
    encode_message,
)


def test_heartbeat_round_trip():
    message = Heartbeat(node_id="lxc1", agent_version="0.2.0")
    decoded = decode_message(encode_message(message), Heartbeat)
    assert decoded.node_id == "lxc1"
    assert decoded.schema_version == "1"


def test_task_request_requires_bounded_expiry():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        TaskRequest(
            node_id="lxc1",
            task_id=1,
            idempotency_key="one",
            source_node_id="vm1",
            intent_json='{"action":"disk-check"}',
            authorization_token="token",
            timestamp=now,
            expires_at=now + timedelta(minutes=16),
        )


def test_task_request_rejects_extra_fields():
    with pytest.raises(ValidationError):
        Heartbeat(node_id="lxc1", agent_version="0.2.0", arbitrary="bad")


def test_ack_and_readiness_messages_are_typed():
    ack = TaskAck(node_id="lxc1", task_id=1, idempotency_key="one", accepted=True)
    assert decode_message(encode_message(ack), TaskAck).accepted is True
    request = ReadinessRequest(
        node_id="lxc1",
        source_node_id="vm1",
        request_id="r1",
        workload="web",
        constraints={"min_free_disk_bytes": 1},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    assert request.workload == "web"


def test_readiness_rejects_unknown_constraints():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        ReadinessRequest(
            node_id="lxc1",
            source_node_id="vm1",
            request_id="r1",
            workload="web",
            constraints={"execute": "anything"},
            timestamp=now,
            expires_at=now + timedelta(minutes=1),
        )


def test_result_payload_is_bounded():
    result = TaskResult(node_id="lxc1", task_id=1, idempotency_key="one", status="succeeded", ok=True)
    assert TaskResult.model_validate_json(result.model_dump_json()).ok is True


def test_capabilities_are_typed():
    report = CapabilityReport(
        node_id="vm2",
        agent_version="0.2.0",
        os_family="Debian",
        package_manager="apt",
        init_system="systemd",
        local_actions=["disk-check"],
    )
    assert report.kind == "capabilities"
