from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchestrator.paths import ANSIBLE_INVENTORY

CHILD_INVENTORY = Path(__file__).resolve().parent.parent / "ansible" / "child_inventory.ini"
ANSIBLE_CONFIG = Path(__file__).resolve().parent.parent / "ansible" / "ansible.cfg"
PLAYBOOK_DIR = Path(__file__).resolve().parent.parent / "data" / "playbooks"


def _ansible_env() -> dict[str, str]:
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_CONFIG)
    venv_path = Path(__file__).resolve().parent.parent / ".venv" / "bin"
    if "PATH" in env:
        env["PATH"] = f"{venv_path}:{env['PATH']}"
    else:
        env["PATH"] = str(venv_path)
    return env


def _inventory(local: bool) -> Path:
    return CHILD_INVENTORY if local else ANSIBLE_INVENTORY


def _playbook_cmd(subcommand: str, inventory: Path, playbook_path: Path, local: bool) -> list[str]:
    # Invoke Ansible through the active interpreter so copied venvs with
    # absolute shebangs remain usable on managed nodes.
    cmd = [sys.executable, "-m", "ansible.cli.playbook"]
    if subcommand:
        cmd.append(subcommand)
    cmd.extend(["-i", str(inventory)])
    if local:
        cmd.extend(["-c", "local"])
    cmd.append(str(playbook_path))
    return cmd


def syntax_check(playbook_path: Path, local: bool = False) -> tuple[bool, str, str]:
    cmd = _playbook_cmd("--syntax-check", _inventory(local), playbook_path, local)
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=30, env=_ansible_env(),
    )
    return result.returncode == 0, result.stdout, result.stderr


def ansible_run(playbook_path: Path, local: bool = False) -> tuple[bool, str, str]:
    cmd = _playbook_cmd("", _inventory(local), playbook_path, local)
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=300, env=_ansible_env(),
    )
    return result.returncode == 0, result.stdout, result.stderr
