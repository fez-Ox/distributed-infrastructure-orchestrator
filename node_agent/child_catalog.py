"""Deterministic action catalog available to distributed child agents.

The catalog intentionally supports useful local remediation, but never accepts
arbitrary shell/code. Every action maps to fixed Ansible modules and bounded
parameters. The orchestrator still decides whether a task is authorized.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from orchestrator.models import ActionType

CHILD_ACTIONS: set[ActionType] = {
    ActionType.service_status,
    ActionType.log_tail,
    ActionType.disk_check,
    ActionType.workload_readiness,
    ActionType.capabilities_report,
    ActionType.install_local_package,
    ActionType.ensure_local_service,
    ActionType.write_local_state,
}

ALLOWED_LOG_DIRS = ("/var/log/", "/opt/mao/logs/")
_SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9_@.\-]+$")
_PACKAGE_RE = re.compile(r"^[a-zA-Z0-9.+_:@*\-]+$")
_STATE_NAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")
_ALLOWED_SERVICE_STATES = {"started", "stopped", "restarted", "reloaded"}
_MAX_CONTENT_LENGTH = 16_384


def validate_params(action: ActionType, params: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if action in (ActionType.service_status, ActionType.ensure_local_service):
        service = params.get("service", "")
        if not service:
            errors.append(f"{action.value}: 'service' is required")
        elif not isinstance(service, str) or not _SERVICE_NAME_RE.fullmatch(service):
            errors.append(f"{action.value}: invalid service name")
        if action == ActionType.ensure_local_service and params.get("state", "started") not in _ALLOWED_SERVICE_STATES:
            errors.append("ensure-local-service: invalid state")
    elif action == ActionType.log_tail:
        path = params.get("path", "")
        if not isinstance(path, str) or not path.startswith(ALLOWED_LOG_DIRS):
            errors.append(f"log-tail: path is not in allowed log dirs")
        lines = params.get("lines", 20)
        if not isinstance(lines, int) or not (1 <= lines <= 500):
            errors.append("log-tail: 'lines' must be between 1 and 500")
    elif action == ActionType.install_local_package:
        package = params.get("package", "")
        if not isinstance(package, str) or not _PACKAGE_RE.fullmatch(package):
            errors.append("install-local-package: invalid package name")
        if params.get("state", "present") not in {"present", "absent"}:
            errors.append("install-local-package: state must be present or absent")
    elif action == ActionType.write_local_state:
        name = params.get("name", "")
        content = params.get("content", "")
        if not isinstance(name, str) or not _STATE_NAME_RE.fullmatch(name):
            errors.append("write-local-state: name must be a simple filename")
        if not isinstance(content, str) or not content:
            errors.append("write-local-state: content is required")
        elif len(content) > _MAX_CONTENT_LENGTH:
            errors.append("write-local-state: content exceeds size limit")
    return errors


def _command_task(name: str, cmd: str) -> dict[str, Any]:
    # Commands are fixed by this module, never supplied by an LLM/request.
    return {"name": name, "ansible.builtin.command": {"cmd": cmd}, "changed_when": False}


def render_child_tasks(intent) -> list[dict[str, Any]]:
    action = intent.action
    params = intent.params or {}
    errors = validate_params(action, params)
    if errors:
        raise ValueError("; ".join(errors))

    if action == ActionType.service_status:
        return [_command_task(f"Check status of service {params['service']}", f"systemctl is-active {params['service']}")]
    if action == ActionType.log_tail:
        return [_command_task(f"Tail approved log {params['path']}", f"tail -n {int(params.get('lines', 20))} {params['path']}")]
    if action == ActionType.disk_check:
        return [_command_task("Check disk usage", "df -h /")]
    if action == ActionType.workload_readiness:
        return [_command_task("Check disk usage", "df -h /"), _command_task("Check memory", "free -m")]
    if action == ActionType.capabilities_report:
        return [_command_task("Report OS info", "cat /etc/os-release")]
    if action == ActionType.install_local_package:
        return [{"name": "Manage approved local package", "ansible.builtin.package": {"name": params["package"], "state": params.get("state", "present")}}]
    if action == ActionType.ensure_local_service:
        return [{"name": "Manage approved local service", "ansible.builtin.service": {"name": params["service"], "state": params.get("state", "started")}}]
    if action == ActionType.write_local_state:
        return [
            {"name": "Ensure child state directory", "ansible.builtin.file": {"path": "/opt/mao/state", "state": "directory", "mode": "0750"}},
            {"name": "Write approved local state", "ansible.builtin.copy": {"dest": f"/opt/mao/state/{params['name']}", "content": params["content"], "mode": "0600"}},
        ]
    raise ValueError(f"not a child action: {action}")


def render_child_playbook(intent) -> str:
    play = {"hosts": "localhost", "gather_facts": False, "tasks": render_child_tasks(intent)}
    return "---\n" + yaml.dump([play], default_flow_style=False, sort_keys=False).strip()
