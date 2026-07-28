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
    conn = sqlite3.connect(path)
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
        """
    )
    conn.commit()


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
