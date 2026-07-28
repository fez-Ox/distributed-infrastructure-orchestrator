from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import BaseLoader, Environment, StrictUndefined

from orchestrator.db import connect

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "ansible" / "templates"
ANSIBLE_INVENTORY = Path(__file__).resolve().parent.parent / "ansible" / "inventory.ini"
ANSIBLE_CONFIG = Path(__file__).resolve().parent.parent / "ansible" / "ansible.cfg"
SNAPSHOT_DIR_BASE = "/var/backups/mao"


def _resolve_targets(node_ids: list[str]) -> list[dict[str, str]]:
    inv_path = ANSIBLE_INVENTORY
    if not inv_path.exists():
        return []
    host_map: dict[str, str] = {}
    with open(inv_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith(";") or line.startswith("["):
                continue
            parts = line.split()
            if not parts:
                continue
            hostname = parts[0]
            ansible_host = hostname
            for token in parts[1:]:
                if token.startswith("ansible_host="):
                    ansible_host = token.split("=", 1)[1]
            host_map[hostname] = ansible_host
    results = []
    for nid in node_ids:
        ah = host_map.get(nid, nid)
        results.append({"node_id": nid, "ansible_host": ah})
    return results


def _parse_inventory_groups() -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    current_group: str | None = None
    inv_path = ANSIBLE_INVENTORY
    if not inv_path.exists():
        return groups
    with open(inv_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                if section.endswith(":vars") or section.endswith(":children"):
                    current_group = None
                else:
                    current_group = section
                    groups.setdefault(current_group, [])
                continue
            if current_group and line:
                hostname = line.split()[0]
                groups.setdefault(current_group, []).append(hostname)
    return groups


def resolve_group_members(group_name: str) -> list[str]:
    groups = _parse_inventory_groups()
    return groups.get(group_name, [])


def resolve_target(target_str: str) -> list[str]:
    members = resolve_group_members(target_str)
    if members:
        return members
    return [target_str]


def parse_hosts_from_playbook(playbook_yaml: str) -> list[str]:
    for line in playbook_yaml.splitlines():
        stripped = line.strip().lstrip("- ")
        if stripped.startswith("hosts:"):
            val = stripped[len("hosts:"):].strip().strip('"').strip("'")
            return resolve_target(val)
    return []


def _render_template(template_name: str, **kwargs: Any) -> str:
    env = Environment(loader=BaseLoader(), undefined=StrictUndefined, variable_start_string="{{", variable_end_string="}}")
    tmpl_path = TEMPLATES_DIR / template_name
    source = tmpl_path.read_text(encoding="utf-8")
    template = env.from_string(source)
    return template.render(**kwargs)


def _ansible_env() -> dict[str, str]:
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_CONFIG)
    venv_path = Path(__file__).resolve().parent.parent / ".venv" / "bin"
    if "PATH" in env:
        env["PATH"] = f"{venv_path}:{env['PATH']}"
    else:
        env["PATH"] = str(venv_path)
    return env


def _run_playbook(playbook_content: str, playbook_path: Path) -> dict[str, Any]:
    playbook_path.write_text(playbook_content, encoding="utf-8")
    env = _ansible_env()
    result = subprocess.run(
        ["ansible-playbook", "--syntax-check", "-i", str(ANSIBLE_INVENTORY), str(playbook_path)],
        capture_output=True, text=True, timeout=30, env=env,
    )
    if result.returncode != 0:
        return {"ok": False, "error": "syntax check failed", "stdout": result.stdout, "stderr": result.stderr}
    result = subprocess.run(
        ["ansible-playbook", "-i", str(ANSIBLE_INVENTORY), str(playbook_path)],
        capture_output=True, text=True, timeout=300, env=env,
    )
    ok = result.returncode == 0
    return {"ok": ok, "stdout": result.stdout, "stderr": result.stderr}


class SnapshotManager:
    def __init__(self, db: sqlite3.Connection | None = None) -> None:
        self.db = db or connect()

    def create(self, task_id: int, node_id: str, step_id: int | None = None) -> int:
        target_host = node_id
        results = _resolve_targets([node_id])
        if results:
            target_host = results[0]["ansible_host"]
        cursor = self.db.execute(
            "INSERT INTO snapshots (task_id, step_id, node_id, target_host, snapshot_path, status) VALUES (?, ?, ?, ?, ?, 'created')",
            (task_id, step_id, node_id, target_host, ""),
        )
        snapshot_id = cursor.lastrowid
        snapshot_path = f"{SNAPSHOT_DIR_BASE}/{snapshot_id}"
        self.db.execute(
            "UPDATE snapshots SET snapshot_path = ? WHERE snapshot_id = ?",
            (snapshot_path, snapshot_id),
        )
        self.db.commit()
        playbook_yaml = _render_template("snapshot.yml.j2", target=node_id, snapshot_path=snapshot_path)
        playbook_path = Path(f"/tmp/mao-snapshot-{snapshot_id}.yml")
        result = _run_playbook(playbook_yaml, playbook_path)
        if not result["ok"]:
            self.db.execute(
                "UPDATE snapshots SET status = 'failed' WHERE snapshot_id = ?",
                (snapshot_id,),
            )
            self.db.commit()
            return snapshot_id
        size_bytes = 0
        for line in result.get("stdout", "").splitlines():
            m = re.search(r'MAO_SIZE=(\d+)', line)
            if m:
                size_bytes = int(m.group(1))
                break
        self.db.execute(
            "UPDATE snapshots SET size_bytes = ? WHERE snapshot_id = ?",
            (size_bytes or None, snapshot_id),
        )
        self.db.commit()
        return snapshot_id

    def rollback(self, snapshot_id: int) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"Snapshot #{snapshot_id} not found"}
        if row["status"] != "created":
            return {"ok": False, "error": f"Snapshot #{snapshot_id} is {row['status']}, cannot rollback"}
        playbook_yaml = _render_template("rollback.yml.j2", target=row["node_id"], snapshot_path=row["snapshot_path"])
        playbook_path = Path(f"/tmp/mao-rollback-{snapshot_id}.yml")
        result = _run_playbook(playbook_yaml, playbook_path)
        if result["ok"]:
            now = datetime.now(timezone.utc).isoformat()
            self.db.execute(
                "UPDATE snapshots SET status = 'rolled_back', rolled_back_at = ? WHERE snapshot_id = ?",
                (now, snapshot_id),
            )
            self.db.commit()
        return {
            "ok": result["ok"],
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "error": result.get("error"),
        }

    def delete(self, snapshot_id: int) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"Snapshot #{snapshot_id} not found"}
        playbook_yaml = _render_template("snapshot-cleanup.yml.j2", target=row["node_id"], snapshot_path=row["snapshot_path"])
        playbook_path = Path(f"/tmp/mao-cleanup-{snapshot_id}.yml")
        result = _run_playbook(playbook_yaml, playbook_path)
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "UPDATE snapshots SET status = 'deleted', deleted_at = ? WHERE snapshot_id = ?",
            (now, snapshot_id),
        )
        self.db.commit()
        return {
            "ok": True,
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
        }

    def get_by_task(self, task_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM snapshots WHERE task_id = ? ORDER BY snapshot_id",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM snapshots ORDER BY snapshot_id").fetchall()
        return [dict(r) for r in rows]

    def get_node_summary(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT node_id, COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS total_bytes FROM snapshots WHERE status IN ('created', 'rolled_back') GROUP BY node_id ORDER BY node_id",
        ).fetchall()
        return [dict(r) for r in rows]

    def prune(self, days: int) -> dict[str, Any]:
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.db.execute(
            "SELECT snapshot_id, node_id, snapshot_path FROM snapshots WHERE status = 'created' AND created_at < ?",
            (cutoff,),
        ).fetchall()
        total_freed = 0
        count = 0
        for r in rows:
            sid = r["snapshot_id"]
            playbook_yaml = _render_template("snapshot-cleanup.yml.j2", target=r["node_id"], snapshot_path=r["snapshot_path"])
            playbook_path = Path(f"/tmp/mao-cleanup-{sid}.yml")
            result = _run_playbook(playbook_yaml, playbook_path)
            now = datetime.now(timezone.utc).isoformat()
            self.db.execute(
                "UPDATE snapshots SET status = 'deleted', deleted_at = ? WHERE snapshot_id = ?",
                (now, sid),
            )
            self.db.commit()
            count += 1
        self.db.commit()
        return {"deleted": count, "freed_bytes": total_freed}
