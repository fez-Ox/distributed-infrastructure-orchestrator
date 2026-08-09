"""Optional live NATS protocol/ACL tests.

Run with NATS_SERVER_BIN=/path/to/nats-server. They are skipped in normal unit
runs so a developer does not need a broker installed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
from pathlib import Path

import nats
import pytest

from scripts.render_nats_config import render


pytestmark = pytest.mark.skipif(
    not (os.environ.get("NATS_SERVER_BIN") or shutil.which("nats-server")),
    reason="set NATS_SERVER_BIN or install nats-server to run live tests",
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_authenticated_subject_permissions(tmp_path, monkeypatch):
    async def scenario() -> None:
        port = free_port()
        monkeypatch.setenv("NATS_ORCHESTRATOR_USER", "orchestrator")
        monkeypatch.setenv("NATS_ORCHESTRATOR_PASSWORD", "orchestrator-password")
        monkeypatch.setenv("NATS_PORT", str(port))
        monkeypatch.setenv(
            "NATS_CHILD_CREDENTIALS_JSON",
            '{"node1":{"user":"child-node1","password":"node1-password"},'
            '"node2":{"user":"child-node2","password":"node2-password"}}',
        )
        config_path = tmp_path / "nats.conf"
        config_path.write_text(render(), encoding="utf-8")
        binary = os.environ.get("NATS_SERVER_BIN") or shutil.which("nats-server")
        assert binary
        server = subprocess.Popen([binary, "-c", str(config_path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        clients = []
        try:
            url = f"nats://127.0.0.1:{port}"
            for _ in range(30):
                try:
                    orchestrator = await nats.connect(url, user="orchestrator", password="orchestrator-password")
                    break
                except Exception:
                    await asyncio.sleep(0.1)
            else:
                stderr = server.stderr.read().decode() if server.stderr else ""
                raise AssertionError(f"NATS did not start: {stderr}")
            clients.append(orchestrator)
            received = asyncio.Event()

            async def on_heartbeat(_):
                received.set()

            await orchestrator.subscribe("agent.*.heartbeat", cb=on_heartbeat)
            child = await nats.connect(url, user="child-node1", password="node1-password")
            clients.append(child)
            await child.publish("agent.node1.heartbeat", b"ok")
            await child.flush()
            await asyncio.wait_for(received.wait(), timeout=2)

            errors: list[Exception] = []

            async def on_error(error):
                errors.append(error)

            restricted = await nats.connect(url, user="child-node1", password="node1-password", error_cb=on_error)
            clients.append(restricted)
            await restricted.publish("agent.node2.heartbeat", b"forbidden")
            await asyncio.sleep(0.2)
            assert errors, "cross-node publish should be rejected by NATS permissions"
        finally:
            for client in clients:
                await client.close()
            server.terminate()
            server.wait(timeout=5)

    asyncio.run(scenario())
