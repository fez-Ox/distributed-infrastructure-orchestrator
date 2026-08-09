"""NATS bridge that connects the orchestrator to child agents.

Runs in a background thread with its own asyncio loop. Listens for
heartbeats, capabilities, status and task results from child agents and
upserts them into the orchestrator SQLite database. Can also publish a
task to a child agent and wait for its result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import nats

from orchestrator.child_protocol import (
    CapabilityReport,
    Heartbeat,
    ReadinessRequest,
    ReadinessResponse,
    StatusEvent,
    TaskAck,
    TaskCancel,
    TaskProgress,
    TaskRequest,
    TaskResult,
    encode_message,
)
from orchestrator.child_token import sign_task
from orchestrator.db import (
    connect,
    finish_child_task,
    get_db_path,
    mark_stale_agents,
    record_child_message,
    register_child_task,
    request_child_cancel,
)


class ChildBridge:
    def __init__(
        self,
        nats_url: str,
        auth_secret: str,
        db_path: str | None = None,
        reconnect_seconds: float = 5.0,
        connect_timeout: float = 5.0,
        nats_user: str = "",
        nats_password: str = "",
        nats_creds_file: str = "",
        source_node_id: str = "vm1",
        stale_after_seconds: float = 45.0,
    ) -> None:
        self.nats_url = nats_url
        self.auth_secret = auth_secret
        self.db_path = db_path or get_db_path()
        self.reconnect_seconds = reconnect_seconds
        self.connect_timeout = connect_timeout
        self.nats_user = nats_user
        self.nats_password = nats_password
        self.nats_creds_file = nats_creds_file
        self.source_node_id = source_node_id
        self.stale_after_seconds = stale_after_seconds
        self.loop: asyncio.AbstractEventLoop | None = None
        self.nc: Any = None
        self.connected: bool = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._results: dict[int, dict[str, Any]] = {}
        self._readiness: dict[str, dict[str, Any]] = {}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, name="child-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.loop and self.nc:
            asyncio.run_coroutine_threadsafe(self.nc.drain(), self.loop)

    def _run_loop(self) -> None:
        # nats-py logs a full traceback ("nats: encountered error") on every
        # failed connect attempt. Suppress it; we surface our own concise
        # message instead of raw connection errors in the REPL.
        logging.getLogger("nats").setLevel(logging.CRITICAL)
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._connect_forever())

    def is_connected(self) -> bool:
        return self.connected and self.nc is not None

    async def _connect_forever(self) -> None:
        notice_shown = False
        while True:
            connection_lost = asyncio.Event()

            async def on_disconnected() -> None:
                self.connected = False

            async def on_reconnected() -> None:
                self.connected = True

            async def on_closed() -> None:
                self.connected = False
                connection_lost.set()

            try:
                connect_kwargs: dict[str, object] = {
                    "disconnected_cb": on_disconnected,
                    "reconnected_cb": on_reconnected,
                    "closed_cb": on_closed,
                }
                if self.nats_creds_file:
                    connect_kwargs["user_credentials"] = self.nats_creds_file
                elif self.nats_user:
                    connect_kwargs.update(user=self.nats_user, password=self.nats_password)
                self.nc = await asyncio.wait_for(
                    nats.connect(self.nats_url, **connect_kwargs), timeout=self.connect_timeout
                )
            except Exception:
                self.connected = False
                if not notice_shown:
                    print(
                        f"child bridge: NATS not reachable at {self.nats_url}; "
                        "child-agent bridge inactive (retrying in the background)"
                    )
                    notice_shown = True
                await asyncio.sleep(self.reconnect_seconds)
                continue
            self.connected = True
            await self.nc.subscribe("agent.*.heartbeat", cb=self._on_heartbeat)
            await self.nc.subscribe("agent.*.capabilities", cb=self._on_capabilities)
            await self.nc.subscribe("agent.*.status", cb=self._on_status)
            await self.nc.subscribe("agent.*.tasks.ack", cb=self._on_ack)
            await self.nc.subscribe("agent.*.tasks.progress", cb=self._on_progress)
            await self.nc.subscribe("agent.*.tasks.result", cb=self._on_result)
            await self.nc.subscribe("agent.*.workload.readiness.response", cb=self._on_readiness)
            asyncio.create_task(self._stale_monitor())
            print(f"child bridge: listening on {self.nats_url}")
            await connection_lost.wait()
            await asyncio.sleep(self.reconnect_seconds)

    @staticmethod
    def _subject_node(subject: str) -> str | None:
        parts = subject.split(".")
        return parts[1] if len(parts) >= 3 and parts[0] == "agent" else None

    def _validate_source_node(self, msg, node_id: str) -> None:
        subject_node = self._subject_node(msg.subject)
        if subject_node != node_id:
            raise ValueError("message node_id does not match NATS subject")

    async def _on_heartbeat(self, msg) -> None:
        try:
            payload = Heartbeat.model_validate_json(msg.data).model_dump(mode="json")
            self._validate_source_node(msg, payload["node_id"])
            if self._accept_protocol_message(payload, "heartbeat"):
                self._upsert_heartbeat(payload)
        except Exception as exc:
            print(f"child bridge: heartbeat error: {exc}")

    async def _on_capabilities(self, msg) -> None:
        try:
            payload = CapabilityReport.model_validate_json(msg.data).model_dump(mode="json")
            self._validate_source_node(msg, payload["node_id"])
            if self._accept_protocol_message(payload, "capabilities"):
                self._upsert_capabilities(payload)
        except Exception as exc:
            print(f"child bridge: capabilities error: {exc}")

    async def _on_status(self, msg) -> None:
        try:
            payload = StatusEvent.model_validate_json(msg.data).model_dump(mode="json")
            self._validate_source_node(msg, payload["node_id"])
            if self._accept_protocol_message(payload, "status"):
                self._upsert_status(payload)
        except Exception as exc:
            print(f"child bridge: status error: {exc}")

    async def _on_ack(self, msg) -> None:
        try:
            payload = TaskAck.model_validate_json(msg.data).model_dump(mode="json")
            self._validate_source_node(msg, payload["node_id"])
            if self._accept_protocol_message(payload, "ack"):
                self._upsert_task_event(payload, "ack")
        except Exception as exc:
            print(f"child bridge: acknowledgement error: {exc}")

    async def _on_progress(self, msg) -> None:
        try:
            payload = TaskProgress.model_validate_json(msg.data).model_dump(mode="json")
            self._validate_source_node(msg, payload["node_id"])
            if self._accept_protocol_message(payload, "progress"):
                self._upsert_task_event(payload, "progress")
        except Exception as exc:
            print(f"child bridge: progress error: {exc}")

    async def _on_readiness(self, msg) -> None:
        try:
            result = ReadinessResponse.model_validate_json(msg.data)
            payload = result.model_dump(mode="json")
            self._validate_source_node(msg, payload["node_id"])
            if not self._accept_protocol_message(payload, "readiness"):
                return
            with self._lock:
                entry = self._readiness.get(result.request_id)
                if entry:
                    entry["payload"] = payload
                    entry["event"].set()
            self._upsert_readiness(payload)
        except Exception as exc:
            print(f"child bridge: readiness error: {exc}")

    async def _on_result(self, msg) -> None:
        try:
            result = TaskResult.model_validate_json(msg.data)
            payload = result.model_dump(mode="json")
        except Exception as exc:
            print(f"child bridge: result validation error: {exc}")
            return
        try:
            self._validate_source_node(msg, payload["node_id"])
        except Exception as exc:
            print(f"child bridge: result source validation error: {exc}")
            return
        if not self._accept_protocol_message(payload, "result"):
            return
        self._upsert_result(payload)
        task_id = payload.get("task_id")
        if task_id is None:
            return
        with self._lock:
            entry = self._results.get(task_id)
        if entry:
            entry["payload"] = payload
            entry["event"].set()

    def _db(self):
        return connect(self.db_path)

    def _accept_protocol_message(self, payload: dict[str, Any], kind: str) -> bool:
        conn = self._db()
        try:
            return record_child_message(conn, payload["message_id"], payload["node_id"], kind)
        finally:
            conn.close()

    def _ensure_node(self, conn, node_id: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO nodes (node_id, hostname, role, enabled) VALUES (?, ?, 'worker', 1)",
            (node_id, node_id),
        )

    def _upsert_heartbeat(self, payload: dict[str, Any]) -> None:
        conn = self._db()
        try:
            node_id = payload["node_id"]
            self._ensure_node(conn, node_id)
            conn.execute(
                "UPDATE nodes SET agent_status = ?, agent_last_heartbeat = ?, agent_version = ?, updated_at = CURRENT_TIMESTAMP WHERE node_id = ?",
                (payload.get("status", "online"), datetime.now(timezone.utc).isoformat(), payload.get("agent_version"), node_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _upsert_capabilities(self, payload: dict[str, Any]) -> None:
        conn = self._db()
        try:
            node_id = payload["node_id"]
            self._ensure_node(conn, node_id)
            conn.execute(
                "UPDATE nodes SET agent_capabilities = ?, agent_version = COALESCE(?, agent_version), agent_status = 'online', updated_at = CURRENT_TIMESTAMP WHERE node_id = ?",
                (json.dumps(payload, sort_keys=True, default=str), payload.get("agent_version"), node_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _upsert_status(self, payload: dict[str, Any]) -> None:
        conn = self._db()
        try:
            node_id = payload["node_id"]
            self._ensure_node(conn, node_id)
            conn.execute(
                "UPDATE nodes SET agent_status = ?, updated_at = CURRENT_TIMESTAMP WHERE node_id = ?",
                (payload.get("status", "online"), node_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _upsert_readiness(self, payload: dict[str, Any]) -> None:
        conn = self._db()
        try:
            node_id = payload["node_id"]
            self._ensure_node(conn, node_id)
            conn.execute(
                "UPDATE nodes SET agent_readiness = ?, agent_readiness_reason = ?, agent_readiness_updated_at = ?, updated_at = CURRENT_TIMESTAMP WHERE node_id = ?",
                ("ready" if payload["ready"] else "not_ready", payload.get("reason", ""), datetime.now(timezone.utc).isoformat(), node_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _upsert_task_event(self, payload: dict[str, Any], event_kind: str) -> None:
        conn = self._db()
        try:
            task_id = payload["task_id"]
            run = conn.execute("SELECT idempotency_key FROM child_task_runs WHERE task_id = ? AND node_id = ?", (task_id, payload["node_id"])).fetchone()
            if run is None or run["idempotency_key"] != payload["idempotency_key"]:
                return
            current = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if current and current["status"] in {"succeeded", "failed", "rejected", "expired"}:
                return
            if event_kind == "ack":
                status = "running" if payload["accepted"] else "rejected"
                error = payload.get("reason")
            else:
                status_map = {"queued": "pending", "running": "running", "verifying": "verifying", "cancelling": "running"}
                status = status_map[payload["status"]]
                error = payload.get("detail")
            conn.execute("UPDATE tasks SET status = ?, error = ?, execution_mode = 'child', updated_at = CURRENT_TIMESTAMP WHERE task_id = ?", (status, error, task_id))
            conn.execute("UPDATE task_steps SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP WHERE task_id = ? AND step_index = 0", (status, error, task_id))
            conn.commit()
        finally:
            conn.close()

    def _upsert_result(self, payload: dict[str, Any]) -> None:
        conn = self._db()
        try:
            task_id = payload.get("task_id")
            node_id = payload.get("node_id")
            if task_id is None or not node_id:
                return
            run = conn.execute("SELECT idempotency_key, status FROM child_task_runs WHERE task_id = ? AND node_id = ?", (task_id, node_id)).fetchone()
            if run is None or run["idempotency_key"] != payload.get("idempotency_key"):
                return
            if run["status"] in {"succeeded", "failed", "rejected", "expired"}:
                return
            status = payload.get("status") or ("succeeded" if payload.get("ok") else "failed")
            error = payload.get("error")
            stdout = payload.get("stdout") or ""
            stderr = payload.get("stderr") or ""
            finish_child_task(conn, task_id, node_id, status, json.dumps(payload, sort_keys=True, default=str))
            conn.execute(
                "UPDATE tasks SET status = ?, error = ?, execution_mode = 'child', updated_at = CURRENT_TIMESTAMP WHERE task_id = ?",
                (status, error, task_id),
            )
            conn.execute(
                "UPDATE task_steps SET status = ?, ansible_stdout = ?, ansible_stderr = ?, error = ?, updated_at = CURRENT_TIMESTAMP WHERE task_id = ? AND step_index = 0",
                (status, stdout, stderr, error, task_id),
            )
            conn.commit()
        finally:
            conn.close()

    async def _stale_monitor(self) -> None:
        while self.connected:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.stale_after_seconds)
            conn = self._db()
            try:
                mark_stale_agents(conn, cutoff.isoformat())
            finally:
                conn.close()
            await asyncio.sleep(min(15.0, max(1.0, self.stale_after_seconds / 3)))

    def send_task(self, node_id: str, task_id: int, intent_json: str) -> str:
        if not self.is_connected():
            raise RuntimeError(
                f"child bridge is not connected to NATS ({self.nats_url}); cannot deliver task"
            )
        token = sign_task(self.auth_secret, task_id, node_id)
        request = TaskRequest(
            node_id=node_id,
            task_id=task_id,
            idempotency_key=f"child-task-{task_id}-{node_id}",
            source_node_id=self.source_node_id,
            intent_json=intent_json,
            authorization_token=token,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        conn = self._db()
        try:
            register_child_task(conn, task_id, node_id, request.idempotency_key)
        finally:
            conn.close()
        with self._lock:
            self._results.setdefault(task_id, {"event": threading.Event(), "payload": None})
        coro = self.nc.publish(f"agent.{node_id}.tasks.request", encode_message(request))
        asyncio.run_coroutine_threadsafe(coro, self.loop)
        return token

    def cancel_task(self, node_id: str, task_id: int, idempotency_key: str | None = None, reason: str = "cancelled by orchestrator") -> None:
        if not self.is_connected():
            raise RuntimeError("child bridge is not connected to NATS")
        request = TaskCancel(
            node_id=node_id,
            task_id=task_id,
            idempotency_key=idempotency_key or f"child-task-{task_id}-{node_id}",
            source_node_id=self.source_node_id,
            reason=reason,
        )
        asyncio.run_coroutine_threadsafe(
            self.nc.publish(f"agent.{node_id}.tasks.cancel", encode_message(request)), self.loop
        )

    def request_readiness(self, node_id: str, workload: str, constraints: dict[str, Any] | None = None) -> str:
        if not self.is_connected():
            raise RuntimeError("child bridge is not connected to NATS")
        request = ReadinessRequest(
            node_id=node_id,
            request_id=f"readiness-{uuid4()}",
            source_node_id=self.source_node_id,
            workload=workload,
            constraints=constraints or {},
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        )
        with self._lock:
            self._readiness[request.request_id] = {"event": threading.Event(), "payload": None}
        asyncio.run_coroutine_threadsafe(
            self.nc.publish(f"agent.{node_id}.workload.readiness.request", encode_message(request)), self.loop
        )
        return request.request_id

    def wait_for_readiness(self, request_id: str, timeout: float = 30.0) -> dict[str, Any] | None:
        with self._lock:
            entry = self._readiness.get(request_id)
        if not entry:
            return None
        if entry["event"].wait(timeout):
            return entry["payload"]
        return None

    def wait_for_result(self, task_id: int, timeout: float = 120.0) -> dict[str, Any] | None:
        with self._lock:
            entry = self._results.setdefault(task_id, {"event": threading.Event(), "payload": None})
        if entry["event"].wait(timeout):
            return entry["payload"]
        return None
