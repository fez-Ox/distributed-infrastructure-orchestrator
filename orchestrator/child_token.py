"""One-time authorization tokens issued to child agents.

A task is only executed by a child agent if it carries a valid HMAC token
signed with the shared CHILD_AUTH_SECRET. Tokens bind a task_id to a
node_id and expire after a short TTL, so a captured token is time-limited.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time


def sign_task(secret: str, task_id: int, node_id: str, ttl: int = 300) -> str:
    payload = json.dumps(
        {"task_id": task_id, "node_id": node_id, "exp": int(time.time()) + ttl},
        sort_keys=True,
        separators=(",", ":"),
    )
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_token(secret: str, token: str, task_id: int, node_id: str) -> bool:
    if not secret or not token:
        return False
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        data = json.loads(payload)
        if data.get("task_id") != task_id:
            return False
        if data.get("node_id") != node_id:
            return False
        if int(data.get("exp", 0)) < time.time():
            return False
        return True
    except Exception:
        return False
