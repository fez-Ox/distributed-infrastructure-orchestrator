from __future__ import annotations

import pytest

from orchestrator.db import connect, init_db
from orchestrator.snapshot import SnapshotManager, resolve_group_members, resolve_target, parse_hosts_from_playbook


class TestInventoryHelpers:
    def test_resolve_known_node(self):
        result = resolve_target("lxc1")
        assert "lxc1" in result

    def test_resolve_known_group(self):
        result = resolve_target("lxcs")
        assert "lxc1" in result
        assert "lxc2" in result

    def test_resolve_managed_nodes(self):
        result = resolve_target("managed_nodes")
        assert "lxc1" in result
        assert "lxc2" in result
        assert "vm2" in result

    def test_resolve_vms_group(self):
        result = resolve_target("vms")
        assert "vm1" in result
        assert "vm2" in result

    def test_resolve_orchestrator_group(self):
        result = resolve_target("orchestrator")
        assert "vm1" in result

    def test_resolve_unknown_name_returns_self(self):
        result = resolve_target("nonexistent")
        assert result == ["nonexistent"]

    def test_parse_hosts_single_node(self):
        yaml = """---
- hosts: lxc1
  gather_facts: no
  tasks:
    - name: test
      ansible.builtin.package:
        name: htop
        state: present
"""
        result = parse_hosts_from_playbook(yaml)
        assert result == ["lxc1"]

    def test_parse_hosts_group(self):
        yaml = """---
- hosts: lxcs
  gather_facts: no
  tasks:
    - name: test
      ansible.builtin.package:
        name: htop
        state: present
"""
        result = parse_hosts_from_playbook(yaml)
        assert "lxc1" in result
        assert "lxc2" in result

    def test_resolve_group_members_lxcs(self):
        members = resolve_group_members("lxcs")
        assert "lxc1" in members
        assert "lxc2" in members
        assert "vm1" not in members


class TestSnapshotManager:
    def test_init_db_has_snapshots_table(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = connect(str(db_path))
        init_db(conn)
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [r["name"] for r in tables]
        assert "snapshots" in table_names

    def test_snapshot_table_columns(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = connect(str(db_path))
        init_db(conn)
        cols = conn.execute("PRAGMA table_info(snapshots)").fetchall()
        col_names = [c["name"] for c in cols]
        assert "snapshot_id" in col_names
        assert "task_id" in col_names
        assert "step_id" in col_names
        assert "node_id" in col_names
        assert "target_host" in col_names
        assert "snapshot_path" in col_names
        assert "size_bytes" in col_names
        assert "status" in col_names
        assert "created_at" in col_names
        assert "rolled_back_at" in col_names
        assert "deleted_at" in col_names
        assert "source_type" not in col_names
        assert "source_id" not in col_names

    def test_create_snapshot_db_row(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = connect(str(db_path))
        init_db(conn)

        monkeypatch.setattr("orchestrator.snapshot.ANSIBLE_INVENTORY", tmp_path / "inventory.ini")
        inv_file = tmp_path / "inventory.ini"
        inv_file.write_text("lxc1 ansible_host=192.0.2.21\n")

        mgr = SnapshotManager(db=conn)
        mgr.create(task_id=1, node_id="lxc1")

        rows = conn.execute("SELECT * FROM snapshots").fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert r["task_id"] == 1
        assert r["step_id"] is None
        assert r["node_id"] == "lxc1"
        assert r["snapshot_path"] == "/var/backups/mao/1"
        assert r["status"] in ("created", "failed")

    def test_create_snapshot_with_step_id(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = connect(str(db_path))
        init_db(conn)

        monkeypatch.setattr("orchestrator.snapshot.ANSIBLE_INVENTORY", tmp_path / "inventory.ini")
        inv_file = tmp_path / "inventory.ini"
        inv_file.write_text("lxc1 ansible_host=192.0.2.21\n")

        mgr = SnapshotManager(db=conn)
        mgr.create(task_id=1, node_id="lxc1", step_id=0)

        rows = conn.execute("SELECT * FROM snapshots").fetchall()
        assert len(rows) == 1
        assert rows[0]["step_id"] == 0

    def test_create_snapshot_from_group_target(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = connect(str(db_path))
        init_db(conn)

        monkeypatch.setattr("orchestrator.snapshot.ANSIBLE_INVENTORY", tmp_path / "inventory.ini")
        inv_file = tmp_path / "inventory.ini"
        inv_file.write_text("[lxcs]\nlxc1\nlxc2\n")

        mgr = SnapshotManager(db=conn)
        node_ids = resolve_target("lxcs")
        for nid in node_ids:
            mgr.create(task_id=1, node_id=nid)

        rows = conn.execute("SELECT * FROM snapshots ORDER BY snapshot_id").fetchall()
        assert len(rows) == 2
        assert rows[0]["node_id"] == "lxc1"
        assert rows[1]["node_id"] == "lxc2"

    def test_get_by_task(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = connect(str(db_path))
        init_db(conn)

        monkeypatch.setattr("orchestrator.snapshot.ANSIBLE_INVENTORY", tmp_path / "inventory.ini")
        inv_file = tmp_path / "inventory.ini"
        inv_file.write_text("lxc1 ansible_host=192.0.2.21\n")

        mgr = SnapshotManager(db=conn)
        mgr.create(task_id=42, node_id="lxc1")

        snaps = mgr.get_by_task(42)
        assert len(snaps) == 1
        assert snaps[0]["task_id"] == 42

    def test_get_by_task_empty(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = connect(str(db_path))
        init_db(conn)
        mgr = SnapshotManager(db=conn)
        snaps = mgr.get_by_task(999)
        assert snaps == []

    def test_get_all(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = connect(str(db_path))
        init_db(conn)

        monkeypatch.setattr("orchestrator.snapshot.ANSIBLE_INVENTORY", tmp_path / "inventory.ini")
        inv_file = tmp_path / "inventory.ini"
        inv_file.write_text("lxc1 ansible_host=192.0.2.21\n")

        mgr = SnapshotManager(db=conn)
        mgr.create(task_id=1, node_id="lxc1")
        mgr.create(task_id=2, node_id="lxc1")

        all_snaps = mgr.get_all()
        assert len(all_snaps) == 2

    def test_get_node_summary(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = connect(str(db_path))
        init_db(conn)

        monkeypatch.setattr("orchestrator.snapshot.ANSIBLE_INVENTORY", tmp_path / "inventory.ini")
        inv_file = tmp_path / "inventory.ini"
        inv_file.write_text("lxc1 ansible_host=192.0.2.21\n")

        conn.execute(
            "INSERT INTO snapshots (task_id, node_id, target_host, snapshot_path, size_bytes, status) VALUES (?, ?, ?, ?, ?, ?)",
            (1, "lxc1", "192.0.2.21", "/var/backups/mao/1", 1024, "created"),
        )
        conn.execute(
            "INSERT INTO snapshots (task_id, node_id, target_host, snapshot_path, size_bytes, status) VALUES (?, ?, ?, ?, ?, ?)",
            (2, "lxc1", "192.0.2.21", "/var/backups/mao/2", 2048, "created"),
        )
        conn.commit()

        mgr = SnapshotManager(db=conn)
        summary = mgr.get_node_summary()
        assert len(summary) == 1
        assert summary[0]["node_id"] == "lxc1"
        assert summary[0]["count"] == 2
        assert summary[0]["total_bytes"] == 3072

    def test_delete_updates_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = connect(str(db_path))
        init_db(conn)

        monkeypatch.setattr("orchestrator.snapshot.ANSIBLE_INVENTORY", tmp_path / "inventory.ini")
        inv_file = tmp_path / "inventory.ini"
        inv_file.write_text("lxc1 ansible_host=192.0.2.21\n")

        monkeypatch.setattr("orchestrator.snapshot._run_playbook", lambda *a, **kw: {"ok": True, "stdout": "", "stderr": ""})

        mgr = SnapshotManager(db=conn)
        mgr.create(task_id=1, node_id="lxc1")
        snap_id = conn.execute("SELECT snapshot_id FROM snapshots").fetchone()["snapshot_id"]

        mgr.delete(snap_id)
        row = conn.execute("SELECT status, deleted_at FROM snapshots WHERE snapshot_id = ?", (snap_id,)).fetchone()
        assert row["status"] == "deleted"
        assert row["deleted_at"] is not None

    def test_rollback_invalid_id(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = connect(str(db_path))
        init_db(conn)
        mgr = SnapshotManager(db=conn)
        result = mgr.rollback(999)
        assert not result["ok"]
        assert "not found" in result["error"]

    def test_rollback_non_created_status(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = connect(str(db_path))
        init_db(conn)
        conn.execute(
            "INSERT INTO snapshots (task_id, node_id, target_host, snapshot_path, status) VALUES (?, ?, ?, ?, ?)",
            (1, "lxc1", "192.0.2.21", "/var/backups/mao/1", "rolled_back"),
        )
        conn.commit()
        mgr = SnapshotManager(db=conn)
        result = mgr.rollback(1)
        assert not result["ok"]
        assert "cannot rollback" in result["error"]
