"""Versioned, validated protocol messages for Option B child agents."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION = "1"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProtocolMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1"] = PROTOCOL_VERSION
    message_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    timestamp: datetime = Field(default_factory=utc_now)


class Heartbeat(ProtocolMessage):
    kind: Literal["heartbeat"] = "heartbeat"
    status: Literal["online", "degraded", "stopping"] = "online"
    agent_version: str = Field(min_length=1, max_length=64)


class CapabilityReport(ProtocolMessage):
    kind: Literal["capabilities"] = "capabilities"
    agent_version: str = Field(min_length=1, max_length=64)
    os_family: str = Field(min_length=1, max_length=64)
    package_manager: str = Field(min_length=1, max_length=64)
    init_system: str = Field(min_length=1, max_length=64)
    local_actions: list[str] = Field(default_factory=list, max_length=128)


class StatusEvent(ProtocolMessage):
    kind: Literal["status"] = "status"
    status: Literal["online", "degraded", "busy", "offline", "stopping"]
    reason: str | None = Field(default=None, max_length=500)


class TaskRequest(ProtocolMessage):
    kind: Literal["task_request"] = "task_request"
    task_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    source_node_id: str = Field(min_length=1, max_length=128)
    intent_json: str = Field(min_length=2, max_length=100_000)
    authorization_token: str = Field(min_length=1, max_length=2_000)
    expires_at: datetime

    @model_validator(mode="after")
    def validate_expiry(self) -> "TaskRequest":
        if self.expires_at <= self.timestamp:
            raise ValueError("expires_at must be after timestamp")
        if self.expires_at - self.timestamp > timedelta(minutes=15):
            raise ValueError("task request lifetime cannot exceed 15 minutes")
        return self


class TaskAck(ProtocolMessage):
    kind: Literal["task_ack"] = "task_ack"
    task_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    accepted: bool
    reason: str | None = Field(default=None, max_length=500)


class TaskProgress(ProtocolMessage):
    kind: Literal["task_progress"] = "task_progress"
    task_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    status: Literal["queued", "running", "verifying", "cancelling"]
    detail: str | None = Field(default=None, max_length=500)


class TaskCancel(ProtocolMessage):
    kind: Literal["task_cancel"] = "task_cancel"
    task_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    source_node_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(default="cancelled by orchestrator", max_length=500)


class ReadinessRequest(ProtocolMessage):
    kind: Literal["readiness_request"] = "readiness_request"
    request_id: str = Field(min_length=1, max_length=128)
    source_node_id: str = Field(min_length=1, max_length=128)
    workload: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    constraints: dict[str, Any] = Field(default_factory=dict, max_length=16)
    expires_at: datetime

    @model_validator(mode="after")
    def validate_expiry(self) -> "ReadinessRequest":
        if self.expires_at <= self.timestamp:
            raise ValueError("expires_at must be after timestamp")
        if self.expires_at - self.timestamp > timedelta(minutes=5):
            raise ValueError("readiness request lifetime cannot exceed 5 minutes")
        unknown = set(self.constraints) - {"min_free_disk_bytes", "max_load_per_cpu"}
        if unknown:
            raise ValueError(f"unsupported readiness constraints: {sorted(unknown)}")
        return self


class ReadinessResponse(ProtocolMessage):
    kind: Literal["readiness_response"] = "readiness_response"
    request_id: str = Field(min_length=1, max_length=128)
    workload: str = Field(min_length=1, max_length=128)
    ready: bool
    reason: str = Field(default="", max_length=500)
    capacity: dict[str, Any] = Field(default_factory=dict)


class TaskResult(ProtocolMessage):
    kind: Literal["task_result"] = "task_result"
    task_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    status: Literal["succeeded", "failed", "rejected", "expired"]
    ok: bool
    stdout: str = Field(default="", max_length=100_000)
    stderr: str = Field(default="", max_length=100_000)
    error: str | None = Field(default=None, max_length=2_000)
    finished_at: datetime = Field(default_factory=utc_now)


Message = Heartbeat | CapabilityReport | StatusEvent | TaskRequest | TaskAck | TaskProgress | TaskCancel | ReadinessRequest | ReadinessResponse | TaskResult


def encode_message(message: Message) -> bytes:
    return message.model_dump_json().encode("utf-8")


def decode_message(payload: bytes, message_type: type[Message]) -> Message:
    return message_type.model_validate_json(payload)


def parse_json_object(payload: bytes) -> dict[str, Any]:
    """Parse a JSON object before selecting its strict message schema."""
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("protocol payload must be a JSON object")
    return value
