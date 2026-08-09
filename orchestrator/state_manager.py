from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import Any

import nats

from orchestrator.db import connect, init_db, upsert_telemetry

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
NATS_USER = os.environ.get("NATS_USER", "")
NATS_PASSWORD = os.environ.get("NATS_PASSWORD", "")
NATS_CREDS_FILE = os.environ.get("NATS_CREDS_FILE", "")


def validate_telemetry(payload: dict[str, Any]) -> None:
    required = ["node_id", "hostname", "timestamp"]
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise ValueError(f"missing required telemetry fields: {missing}")


async def run() -> None:
    conn = connect()
    init_db(conn)

    connect_kwargs: dict[str, object] = {}
    if NATS_CREDS_FILE:
        connect_kwargs["user_credentials"] = NATS_CREDS_FILE
    elif NATS_USER:
        connect_kwargs.update(user=NATS_USER, password=NATS_PASSWORD)
    nc = await nats.connect(NATS_URL, **connect_kwargs)
    stop_event = asyncio.Event()

    async def handler(msg):
        try:
            payload = json.loads(msg.data.decode("utf-8"))
            validate_telemetry(payload)
            upsert_telemetry(conn, payload)
        except Exception as exc:  # keep service alive on malformed telemetry
            print(f"failed to process {msg.subject}: {exc}")

    await nc.subscribe("telemetry.*.system", cb=handler)
    print(f"state manager listening on {NATS_URL}")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    await nc.drain()
    conn.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
