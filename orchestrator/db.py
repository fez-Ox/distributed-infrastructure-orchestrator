from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = "./data/cluster_state.db"


def get_db_path() -> str:
    return os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = Path(db_path or get_db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nodes (
            node_id TEXT PRIMARY KEY,
            hostname TEXT,
            ip TEXT,
            node_type TEXT,
            role TEXT,
            enabled INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            agent_status TEXT,
            agent_capabilities TEXT,
            agent_last_heartbeat TEXT,
            agent_version TEXT,
            agent_readiness TEXT,
            agent_readiness_reason TEXT,
            agent_readiness_updated_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS telemetry_latest (
            node_id TEXT PRIMARY KEY,
            cpu_percent REAL,
            ram_total_bytes INTEGER,
            ram_used_bytes INTEGER,
            ram_percent REAL,
            disk_total_bytes INTEGER,
            disk_used_bytes INTEGER,
            disk_percent REAL,
            uptime_seconds INTEGER,
            payload_json TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id)
        );

        CREATE TABLE IF NOT EXISTS tasks (
            task_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_request TEXT NOT NULL,
            task_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            intent_json TEXT,
            plan_json TEXT,
            target_node_id TEXT,
            error TEXT,
            verifier_result TEXT,
            verifier_reason TEXT,
            verifier_timestamp TEXT,
            policy_verdict TEXT,
            policy_reason TEXT,
            execution_mode TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS task_steps (
            task_id INTEGER NOT NULL,
            step_index INTEGER NOT NULL,
            description TEXT NOT NULL,
            playbook_yaml TEXT NOT NULL,
            playbook_path TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            ansible_stdout TEXT,
            ansible_stderr TEXT,
            error TEXT,
            rollback_playbook_yaml TEXT,
            rollback_status TEXT NOT NULL DEFAULT 'pending',
            rollback_ansible_stdout TEXT,
            rollback_ansible_stderr TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (task_id, step_index),
            FOREIGN KEY (task_id) REFERENCES tasks(task_id)
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            step_id INTEGER,
            node_id TEXT NOT NULL,
            target_host TEXT NOT NULL,
            snapshot_path TEXT NOT NULL,
            distro TEXT,
            size_bytes INTEGER,
            status TEXT NOT NULL DEFAULT 'created',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            rolled_back_at TEXT,
            deleted_at TEXT,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id)
        );

        CREATE TABLE IF NOT EXISTS child_protocol_messages (
            message_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS child_task_runs (
            task_id INTEGER NOT NULL,
            node_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'received',
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            cancel_reason TEXT,
            result_json TEXT,
            received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (task_id, node_id),
            UNIQUE (node_id, idempotency_key)
        );
        """
    )
    conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema. Idempotent for old DBs."""
    node_cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    for col, ddl in {
        "agent_capabilities": "TEXT",
        "agent_last_heartbeat": "TEXT",
        "agent_version": "TEXT",
    }.items():
        if col not in node_cols:
            conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} {ddl}")

    task_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "execution_mode" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN execution_mode TEXT")

    node_cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    for col in ("agent_readiness", "agent_readiness_reason", "agent_readiness_updated_at"):
        if col not in node_cols:
            conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} TEXT")

    step_cols = {r[1] for r in conn.execute("PRAGMA table_info(task_steps)").fetchall()}
    for col, ddl in {
        "rollback_playbook_yaml": "TEXT",
        "rollback_status": "TEXT NOT NULL DEFAULT 'pending'",
        "rollback_ansible_stdout": "TEXT",
        "rollback_ansible_stderr": "TEXT",
    }.items():
        if col not in step_cols:
            conn.execute(f"ALTER TABLE task_steps ADD COLUMN {col} {ddl}")

    child_cols = {r[1] for r in conn.execute("PRAGMA table_info(child_task_runs)").fetchall()}
    for col, ddl in {
        "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        "cancel_reason": "TEXT",
    }.items():
        if col not in child_cols:
            conn.execute(f"ALTER TABLE child_task_runs ADD COLUMN {col} {ddl}")

    conn.commit()


def register_child_task(conn: sqlite3.Connection, task_id: int, node_id: str, idempotency_key: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO child_task_runs (task_id, node_id, idempotency_key, status) VALUES (?, ?, ?, 'dispatched')",
        (task_id, node_id, idempotency_key),
    )
    conn.commit()


def claim_child_task(
    conn: sqlite3.Connection,
    task_id: int,
    node_id: str,
    idempotency_key: str,
) -> tuple[bool, str | None]:
    """Claim a child task once; return (claimed, prior_result_json)."""
    row = conn.execute(
        "SELECT status, result_json FROM child_task_runs WHERE task_id = ? AND node_id = ?",
        (task_id, node_id),
    ).fetchone()
    if row is not None:
        return False, row["result_json"]
    try:
        conn.execute(
            "INSERT INTO child_task_runs (task_id, node_id, idempotency_key, status) VALUES (?, ?, ?, 'running')",
            (task_id, node_id, idempotency_key),
        )
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT result_json FROM child_task_runs WHERE task_id = ? AND node_id = ?",
            (task_id, node_id),
        ).fetchone()
        return False, row["result_json"] if row else None


def finish_child_task(conn: sqlite3.Connection, task_id: int, node_id: str, status: str, result_json: str) -> None:
    conn.execute(
        "UPDATE child_task_runs SET status = ?, result_json = ?, updated_at = CURRENT_TIMESTAMP WHERE task_id = ? AND node_id = ?",
        (status, result_json, task_id, node_id),
    )
    conn.commit()


def request_child_cancel(conn: sqlite3.Connection, task_id: int, node_id: str, reason: str) -> bool:
    cursor = conn.execute(
        "UPDATE child_task_runs SET cancel_requested = 1, cancel_reason = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE task_id = ? AND node_id = ? AND status IN ('received', 'running')",
        (reason, task_id, node_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def child_cancel_requested(conn: sqlite3.Connection, task_id: int, node_id: str) -> tuple[bool, str | None]:
    row = conn.execute(
        "SELECT cancel_requested, cancel_reason FROM child_task_runs WHERE task_id = ? AND node_id = ?",
        (task_id, node_id),
    ).fetchone()
    return (bool(row["cancel_requested"]), row["cancel_reason"]) if row else (False, None)


def record_child_message(conn: sqlite3.Connection, message_id: str, node_id: str, kind: str) -> bool:
    try:
        conn.execute(
            "INSERT INTO child_protocol_messages (message_id, node_id, kind) VALUES (?, ?, ?)",
            (message_id, node_id, kind),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def mark_stale_agents(conn: sqlite3.Connection, stale_before: str) -> int:
    cursor = conn.execute(
        "UPDATE nodes SET agent_status = 'stale', updated_at = CURRENT_TIMESTAMP "
        "WHERE agent_last_heartbeat IS NOT NULL AND agent_last_heartbeat < ? AND agent_status IN ('online', 'busy', 'degraded')",
        (stale_before,),
    )
    conn.commit()
    return cursor.rowcount


def upsert_telemetry(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    node_id = str(payload["node_id"])
    hostname = payload.get("hostname")
    ip = payload.get("ip")
    node_type = payload.get("node_type")
    role = payload.get("role", "worker")
    observed_at = payload.get("timestamp")

    conn.execute(
        """
        INSERT INTO nodes (node_id, hostname, ip, node_type, role, enabled, last_seen, updated_at)
        VALUES (?, ?, ?, ?, ?, 0, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(node_id) DO UPDATE SET
            hostname=excluded.hostname,
            ip=COALESCE(excluded.ip, nodes.ip),
            node_type=COALESCE(excluded.node_type, nodes.node_type),
            role=COALESCE(excluded.role, nodes.role),
            last_seen=excluded.last_seen,
            updated_at=CURRENT_TIMESTAMP
        """,
        (node_id, hostname, ip, node_type, role, observed_at),
    )

    conn.execute(
        """
        INSERT INTO telemetry_latest (
            node_id, cpu_percent, ram_total_bytes, ram_used_bytes, ram_percent,
            disk_total_bytes, disk_used_bytes, disk_percent, uptime_seconds,
            payload_json, observed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(node_id) DO UPDATE SET
            cpu_percent=excluded.cpu_percent,
            ram_total_bytes=excluded.ram_total_bytes,
            ram_used_bytes=excluded.ram_used_bytes,
            ram_percent=excluded.ram_percent,
            disk_total_bytes=excluded.disk_total_bytes,
            disk_used_bytes=excluded.disk_used_bytes,
            disk_percent=excluded.disk_percent,
            uptime_seconds=excluded.uptime_seconds,
            payload_json=excluded.payload_json,
            observed_at=excluded.observed_at
        """,
        (
            node_id,
            payload.get("cpu_percent"),
            payload.get("ram_total_bytes"),
            payload.get("ram_used_bytes"),
            payload.get("ram_percent"),
            payload.get("disk_total_bytes"),
            payload.get("disk_used_bytes"),
            payload.get("disk_percent"),
            payload.get("uptime_seconds"),
            json.dumps(payload, sort_keys=True),
            observed_at,
        ),
    )
    conn.commit()


def main() -> None:
    conn = connect()
    init_db(conn)
    print(f"initialized database: {get_db_path()}")


if __name__ == "__main__":
    main()
