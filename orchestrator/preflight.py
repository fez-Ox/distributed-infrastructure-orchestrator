from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from functools import partial
from pathlib import Path
from typing import Any

from orchestrator.models import ActionType
from orchestrator.paths import ANSIBLE_INVENTORY


class PreflightError(Exception):
    pass


ANSIBLE_CONFIG = Path(__file__).resolve().parent.parent / "ansible" / "ansible.cfg"


ACTION_REQUIREMENTS: dict[ActionType, dict[str, list[str]]] = {
    ActionType.install_pkg: {"package_manager": ["apt", "apk", "dnf", "yum"]},
    ActionType.update_system: {"package_manager": ["apt", "apk", "dnf", "yum"]},
    ActionType.restart_service: {"init_system": ["systemd", "openrc"]},
    ActionType.add_user: {},
    ActionType.config_file: {},
    ActionType.freeform: {},
}


def _ansible_env() -> dict[str, str]:
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_CONFIG)
    venv_path = Path(__file__).resolve().parent.parent / ".venv" / "bin"
    if "PATH" in env:
        env["PATH"] = f"{venv_path}:{env['PATH']}"
    else:
        env["PATH"] = str(venv_path)
    return env


def _parse_inventory_hosts() -> set[str]:
    inv_path = ANSIBLE_INVENTORY
    if not inv_path.exists():
        return set()
    hosts: set[str] = set()
    with open(inv_path) as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith(";") or stripped.startswith("["):
                continue
            hostname = stripped.split()[0]
            hosts.add(hostname)
    return hosts


def _detect_node_capabilities(node_id: str) -> dict[str, str]:
    env = _ansible_env()
    try:
        result = subprocess.run(
            ["ansible", node_id, "-i", str(ANSIBLE_INVENTORY), "-m", "raw", "-a", "cat /etc/os-release 2>/dev/null; command -v doas 2>/dev/null; command -v systemctl 2>/dev/null"],
            capture_output=True, text=True, timeout=15, env=env,
        )
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        raise PreflightError(f"Timed out detecting capabilities for {node_id}")
    except Exception as e:
        raise PreflightError(f"Failed to detect capabilities for {node_id}: {e}")

    caps: dict[str, str] = {}

    if re.search(r'ID=alpine', output):
        caps["os_family"] = "Alpine"
        caps["package_manager"] = "apk"
        caps["init_system"] = "openrc"
    elif re.search(r'ID=debian|ID=ubuntu', output):
        caps["os_family"] = "Debian"
        caps["package_manager"] = "apt"
        caps["init_system"] = "systemd"
    elif re.search(r'ID=fedora|ID=rhel|ID=centos', output):
        caps["os_family"] = "RedHat"
        caps["package_manager"] = "dnf"
        caps["init_system"] = "systemd"
    else:
        caps["os_family"] = "unknown"
        caps["package_manager"] = "unknown"
        caps["init_system"] = "unknown"

    if "doas" in output:
        caps["init_system"] = "openrc"

    return caps


class PreflightEngine:
    def __init__(
        self,
        db: sqlite3.Connection,
        mode: str = "orchestrator",
        child_node_id: str | None = None,
    ) -> None:
        self.db = db
        self.mode = mode
        self.child_node_id = child_node_id

    def check_node_enabled(self, node_id: str) -> None:
        row = self.db.execute(
            "SELECT enabled FROM nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row is None:
            raise PreflightError(f"Node '{node_id}' not found in database")
        if not row["enabled"]:
            raise PreflightError(f"Node '{node_id}' is disabled")

    def check_inventory(self, node_id: str) -> None:
        hosts = _parse_inventory_hosts()
        if node_id not in hosts:
            raise PreflightError(f"Node '{node_id}' not found in Ansible inventory")

    def check_reachable(self, node_id: str) -> None:
        env = _ansible_env()
        try:
            result = subprocess.run(
                ["ansible", node_id, "-i", str(ANSIBLE_INVENTORY), "-m", "ping"],
                capture_output=True, text=True, timeout=15, env=env,
            )
            if result.returncode != 0:
                raise PreflightError(f"Node '{node_id}' is not reachable via SSH")
        except subprocess.TimeoutExpired:
            raise PreflightError(f"Timed out pinging node '{node_id}'")
        except Exception as e:
            raise PreflightError(f"Failed to ping node '{node_id}': {e}")

    def check_os_compat(self, node_id: str, action_type: ActionType) -> None:
        requirements = ACTION_REQUIREMENTS.get(action_type)
        if not requirements:
            return

        try:
            caps = _detect_node_capabilities(node_id)
        except PreflightError:
            raise

        for required_key, allowed_values in requirements.items():
            actual = caps.get(required_key, "unknown")
            if actual not in allowed_values:
                raise PreflightError(
                    f"Node '{node_id}' has {required_key}='{actual}', "
                    f"but action '{action_type.value}' requires one of {allowed_values}"
                )

    def check_all(self, node_id: str, action_type: ActionType) -> list[PreflightError]:
        if self.mode == "child":
            allowed = {self.child_node_id, "localhost"}
            if node_id not in allowed:
                return [PreflightError(
                    f"Child agent can only target itself ('{self.child_node_id}' or 'localhost'), not '{node_id}'"
                )]
            return []
        errors: list[PreflightError] = []
        checks = [
            self.check_node_enabled,
            self.check_inventory,
            self.check_reachable,
            partial(self.check_os_compat, action_type=action_type),
        ]
        for check in checks:
            try:
                check(node_id)
            except PreflightError as e:
                errors.append(e)
        return errors


def resolve_targets(target_str: str) -> list[str]:
    from orchestrator.snapshot import resolve_target
    return resolve_target(target_str)
