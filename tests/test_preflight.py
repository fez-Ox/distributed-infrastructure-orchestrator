from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator.db import connect, init_db


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "test.db"
    conn = connect(str(path))
    init_db(conn)
    conn.execute(
        "INSERT INTO nodes (node_id, hostname, ip, node_type, role, enabled) VALUES (?, ?, ?, ?, ?, ?)",
        ("lxc1", "lxc1", "10.0.0.11", "lxc", "worker", 1),
    )
    conn.execute(
        "INSERT INTO nodes (node_id, hostname, ip, node_type, role, enabled) VALUES (?, ?, ?, ?, ?, ?)",
        ("disabled-node", "disabled-node", "10.0.0.99", "lxc", "worker", 0),
    )
    conn.commit()
    return conn


@pytest.fixture
def inventory(tmp_path: Path) -> Path:
    inv = tmp_path / "inventory.ini"
    inv.write_text("[managed_nodes]\nlxc1\nlxc2\nvm2\n")
    return inv


class TestPreflightEngine:
    def test_node_enabled_in_db(self, db):
        from orchestrator.preflight import PreflightEngine

        engine = PreflightEngine(db)
        engine.check_node_enabled("lxc1")

    def test_node_disabled_raises(self, db):
        from orchestrator.preflight import PreflightEngine, PreflightError

        engine = PreflightEngine(db)
        with pytest.raises(PreflightError, match="disabled"):
            engine.check_node_enabled("disabled-node")

    def test_node_not_in_db_raises(self, db):
        from orchestrator.preflight import PreflightEngine, PreflightError

        engine = PreflightEngine(db)
        with pytest.raises(PreflightError, match="not found"):
            engine.check_node_enabled("nonexistent")

    def test_inventory_host_found(self, db, inventory, monkeypatch):
        from orchestrator.preflight import PreflightEngine

        monkeypatch.setattr("orchestrator.preflight.ANSIBLE_INVENTORY", inventory)
        engine = PreflightEngine(db)
        engine.check_inventory("lxc1")

    def test_inventory_host_not_found_raises(self, db, inventory, monkeypatch):
        from orchestrator.preflight import PreflightEngine, PreflightError

        monkeypatch.setattr("orchestrator.preflight.ANSIBLE_INVENTORY", inventory)
        engine = PreflightEngine(db)
        with pytest.raises(PreflightError, match="not found in Ansible inventory"):
            engine.check_inventory("nonexistent")

    def test_reachable_passes(self, db, monkeypatch):
        from orchestrator.preflight import PreflightEngine

        def mock_run(*args, **kwargs):
            import subprocess
            return subprocess.CompletedProcess([], 0, stdout="lxc1 | SUCCESS", stderr="")

        monkeypatch.setattr("subprocess.run", mock_run)
        engine = PreflightEngine(db)
        engine.check_reachable("lxc1")

    def test_unreachable_raises(self, db, monkeypatch):
        from orchestrator.preflight import PreflightEngine, PreflightError

        def mock_run(*args, **kwargs):
            import subprocess
            return subprocess.CompletedProcess([], 1, stdout="", stderr="UNREACHABLE")

        monkeypatch.setattr("subprocess.run", mock_run)
        engine = PreflightEngine(db)
        with pytest.raises(PreflightError, match="not reachable"):
            engine.check_reachable("lxc1")

    def test_timeout_raises(self, db, monkeypatch):
        import subprocess

        from orchestrator.preflight import PreflightEngine, PreflightError

        def mock_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="ansible", timeout=15)

        monkeypatch.setattr("subprocess.run", mock_run)
        engine = PreflightEngine(db)
        with pytest.raises(PreflightError, match="Timed out"):
            engine.check_reachable("lxc1")

    def test_os_compat_debian(self, db, monkeypatch):
        from orchestrator.models import ActionType
        from orchestrator.preflight import PreflightEngine

        def mock_detect(node_id):
            return {"os_family": "Debian", "package_manager": "apt", "init_system": "systemd"}

        monkeypatch.setattr("orchestrator.preflight._detect_node_capabilities", mock_detect)
        engine = PreflightEngine(db)
        engine.check_os_compat("lxc1", ActionType.install_pkg)

    def test_os_compat_alpine_apk(self, db, monkeypatch):
        from orchestrator.models import ActionType
        from orchestrator.preflight import PreflightEngine

        def mock_detect(node_id):
            return {"os_family": "Alpine", "package_manager": "apk", "init_system": "openrc"}

        monkeypatch.setattr("orchestrator.preflight._detect_node_capabilities", mock_detect)
        engine = PreflightEngine(db)
        engine.check_os_compat("lxc1", ActionType.install_pkg)

    def test_os_compat_wrong_pkg_manager_raises(self, db, monkeypatch):
        from orchestrator.models import ActionType
        from orchestrator.preflight import PreflightEngine, PreflightError

        def mock_detect(node_id):
            return {"os_family": "Unknown", "package_manager": "rpm", "init_system": "unknown"}

        monkeypatch.setattr("orchestrator.preflight._detect_node_capabilities", mock_detect)
        engine = PreflightEngine(db)
        with pytest.raises(PreflightError, match="package_manager='rpm'.*requires one of"):
            engine.check_os_compat("lxc1", ActionType.install_pkg)

    def test_check_all_aggregates_errors(self, db, monkeypatch):
        from orchestrator.models import ActionType
        from orchestrator.preflight import PreflightEngine, PreflightError

        monkeypatch.setattr("orchestrator.preflight.ANSIBLE_INVENTORY", Path("/nonexistent"))

        engine = PreflightEngine(db)
        errors = engine.check_all("nonexistent", ActionType.install_pkg)
        assert len(errors) >= 1

    def test_check_all_passes(self, db, monkeypatch):
        from orchestrator.models import ActionType
        from orchestrator.preflight import PreflightEngine

        monkeypatch.setattr("orchestrator.preflight.ANSIBLE_INVENTORY", Path("/nonexistent"))

        def mock_ping(*args, **kwargs):
            import subprocess
            return subprocess.CompletedProcess([], 0, stdout="lxc1 | SUCCESS", stderr="")

        monkeypatch.setattr("subprocess.run", mock_ping)

        def mock_detect(node_id):
            return {"os_family": "Debian", "package_manager": "apt", "init_system": "systemd"}

        monkeypatch.setattr("orchestrator.preflight._detect_node_capabilities", mock_detect)
        engine = PreflightEngine(db)
        engine.check_node_enabled("lxc1")


class TestDetectCapabilities:
    def test_detect_alpine(self, monkeypatch):
        from orchestrator.preflight import _detect_node_capabilities

        def mock_run(*args, **kwargs):
            import subprocess
            return subprocess.CompletedProcess(
                [],
                0,
                stdout='NAME="Alpine Linux"\nID=alpine\nVERSION_ID=3.23.2\n',
                stderr="",
            )

        monkeypatch.setattr("subprocess.run", mock_run)
        caps = _detect_node_capabilities("lxc1")
        assert caps["os_family"] == "Alpine"
        assert caps["package_manager"] == "apk"
        assert caps["init_system"] == "openrc"

    def test_detect_debian(self, monkeypatch):
        from orchestrator.preflight import _detect_node_capabilities

        def mock_run(*args, **kwargs):
            import subprocess
            return subprocess.CompletedProcess(
                [],
                0,
                stdout='PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\n',
                stderr="",
            )

        monkeypatch.setattr("subprocess.run", mock_run)
        caps = _detect_node_capabilities("vm2")
        assert caps["os_family"] == "Debian"
        assert caps["package_manager"] == "apt"
        assert caps["init_system"] == "systemd"

    def test_detect_unknown_os(self, monkeypatch):
        from orchestrator.preflight import _detect_node_capabilities

        def mock_run(*args, **kwargs):
            import subprocess
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", mock_run)
        caps = _detect_node_capabilities("unknown")
        assert caps["os_family"] == "unknown"

    def test_timeout_raises(self, monkeypatch):
        import subprocess

        from orchestrator.preflight import PreflightError, _detect_node_capabilities

        def mock_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="ansible", timeout=15)

        monkeypatch.setattr("subprocess.run", mock_run)
        with pytest.raises(PreflightError, match="Timed out"):
            _detect_node_capabilities("lxc1")


class TestParseInventory:
    def test_parse_hosts(self, tmp_path, monkeypatch):
        from orchestrator.preflight import ANSIBLE_INVENTORY, _parse_inventory_hosts

        inv = tmp_path / "inventory.ini"
        inv.write_text("[managed_nodes]\nlxc1\nlxc2\nvm2\n\n[orchestrator]\nvm1\n")
        monkeypatch.setattr("orchestrator.preflight.ANSIBLE_INVENTORY", inv)
        hosts = _parse_inventory_hosts()
        assert "lxc1" in hosts
        assert "lxc2" in hosts
        assert "vm2" in hosts
        assert "vm1" in hosts

    def test_parse_hosts_skips_comments(self, tmp_path, monkeypatch):
        from orchestrator.preflight import _parse_inventory_hosts

        inv = tmp_path / "inventory.ini"
        inv.write_text("# comment\n[group]\n; also comment\nlxc1\n")
        monkeypatch.setattr("orchestrator.preflight.ANSIBLE_INVENTORY", inv)
        hosts = _parse_inventory_hosts()
        assert "lxc1" in hosts
        assert len(hosts) == 1

    def test_missing_inventory_returns_empty(self, monkeypatch):
        from orchestrator.preflight import _parse_inventory_hosts

        monkeypatch.setattr("orchestrator.preflight.ANSIBLE_INVENTORY", Path("/nonexistent"))
        hosts = _parse_inventory_hosts()
        assert hosts == set()
