#!/usr/bin/env python3
"""Render an authenticated NATS server config without committing credentials.

Inputs:
  NATS_ORCHESTRATOR_USER / NATS_ORCHESTRATOR_PASSWORD
  NATS_CHILD_CREDENTIALS_JSON='{"node":{"user":"...","password":"..."}}'
  NATS_CHILD_CREDENTIALS_FILE (optional JSON file, preferred for secrets)
  NATS_TELEMETRY_CREDENTIALS_JSON / NATS_TELEMETRY_CREDENTIALS_FILE (optional per-node telemetry users)

The generated config grants each child access only to its own subjects.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


def quote(value: str) -> str:
    return json.dumps(value)


def load_credentials(env_json: str, env_file: str, label: str) -> dict[str, dict[str, str]]:
    file_path = os.environ.get(env_file, "")
    raw = Path(file_path).read_text(encoding="utf-8") if file_path else os.environ.get(env_json, "{}")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{label} credentials must be a JSON object")
    result: dict[str, dict[str, str]] = {}
    for node_id, credentials in data.items():
        if not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id) or "*" in node_id or ">" in node_id:
            raise ValueError(f"invalid node ID: {node_id!r}")
        if not isinstance(credentials, dict) or not credentials.get("user") or not credentials.get("password"):
            raise ValueError(f"credentials for {node_id} require user and password")
        result[node_id] = {"user": str(credentials["user"]), "password": str(credentials["password"])}
    return result


def load_children() -> dict[str, dict[str, str]]:
    return load_credentials("NATS_CHILD_CREDENTIALS_JSON", "NATS_CHILD_CREDENTIALS_FILE", "child")


def load_telemetry() -> dict[str, dict[str, str]]:
    return load_credentials("NATS_TELEMETRY_CREDENTIALS_JSON", "NATS_TELEMETRY_CREDENTIALS_FILE", "telemetry")


def render() -> str:
    orchestrator_user = os.environ.get("NATS_ORCHESTRATOR_USER", "")
    orchestrator_password = os.environ.get("NATS_ORCHESTRATOR_PASSWORD", "")
    if not orchestrator_user or not orchestrator_password:
        raise ValueError("NATS_ORCHESTRATOR_USER and NATS_ORCHESTRATOR_PASSWORD are required")
    children = load_children()
    telemetry = load_telemetry()
    unknown_telemetry = set(telemetry) - set(children)
    if unknown_telemetry:
        raise ValueError(f"telemetry credentials have unknown nodes: {sorted(unknown_telemetry)}")
    port = os.environ.get("NATS_PORT", "4222")
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        raise ValueError("NATS_PORT must be between 1 and 65535")

    lines = [f"listen: \"0.0.0.0:{port}\"", "authorization {", "  users = ["]
    orchestrator_publish = [
        "agent.*.tasks.request",
        "agent.*.tasks.cancel",
        "agent.*.workload.readiness.request",
    ]
    orchestrator_subscribe = [
        "agent.*.heartbeat",
        "agent.*.capabilities",
        "agent.*.status",
        "agent.*.tasks.ack",
        "agent.*.tasks.progress",
        "agent.*.tasks.result",
        "agent.*.workload.readiness.response",
        "telemetry.*.system",
    ]
    lines.extend([
        "    {",
        f"      user: {quote(orchestrator_user)},",
        f"      password: {quote(orchestrator_password)},",
        "      permissions: {",
        f"        publish: [{', '.join(quote(s) for s in orchestrator_publish)}],",
        f"        subscribe: [{', '.join(quote(s) for s in orchestrator_subscribe)}]",
        "      }",
        "    },",
    ])

    child_items = list(children.items())
    for index, (node_id, credentials) in enumerate(child_items):
        comma = "," if index < len(child_items) - 1 or telemetry else ""
        publish = [
            f"agent.{node_id}.heartbeat",
            f"agent.{node_id}.capabilities",
            f"agent.{node_id}.status",
            f"agent.{node_id}.tasks.ack",
            f"agent.{node_id}.tasks.progress",
            f"agent.{node_id}.tasks.result",
            f"agent.{node_id}.workload.readiness.response",
        ]
        subscribe = [
            f"agent.{node_id}.tasks.request",
            f"agent.{node_id}.tasks.cancel",
            f"agent.{node_id}.workload.readiness.request",
        ]
        lines.extend([
            "    {",
            f"      user: {quote(credentials['user'])},",
            f"      password: {quote(credentials['password'])},",
            "      permissions: {",
            f"        publish: [{', '.join(quote(s) for s in publish)}],",
            f"        subscribe: [{', '.join(quote(s) for s in subscribe)}]",
            f"      }}{comma}",
            "    }," if comma else "    }",
        ])

    telemetry_items = list(telemetry.items())
    for index, (node_id, credentials) in enumerate(telemetry_items):
        comma = "," if index < len(telemetry_items) - 1 else ""
        lines.extend([
            "    {",
            f"      user: {quote(credentials['user'])},",
            f"      password: {quote(credentials['password'])},",
            "      permissions: {",
            f"        publish: [{quote(f'telemetry.{node_id}.system')}],",
            "        subscribe: []",
            f"      }}{comma}",
            "    }," if comma else "    }",
        ])
    lines.extend(["  ]", "}", ""])
    return "\n".join(lines)


def main() -> int:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "data/nats-server.conf")
    try:
        rendered = render()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    output.chmod(0o600)
    print(f"wrote authenticated NATS config to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
