from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

ANSIBLE_INVENTORY = Path(__file__).resolve().parent.parent / "ansible" / "inventory.ini"
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


def syntax_check(playbook_path: Path) -> tuple[bool, str, str]:
    result = subprocess.run(
        ["ansible-playbook", "--syntax-check", "-i", str(ANSIBLE_INVENTORY), str(playbook_path)],
        capture_output=True, text=True, timeout=30, env=_ansible_env(),
    )
    return result.returncode == 0, result.stdout, result.stderr


def ansible_run(playbook_path: Path) -> tuple[bool, str, str]:
    result = subprocess.run(
        ["ansible-playbook", "-i", str(ANSIBLE_INVENTORY), str(playbook_path)],
        capture_output=True, text=True, timeout=300, env=_ansible_env(),
    )
    return result.returncode == 0, result.stdout, result.stderr
