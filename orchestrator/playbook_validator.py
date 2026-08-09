from __future__ import annotations

import re
from typing import Any

import yaml

ALLOWED_MODULES_TEMPLATE: set[str] = {
    "ansible.builtin.package",
    "ansible.builtin.apt",
    "ansible.builtin.yum",
    "ansible.builtin.dnf",
    "ansible.builtin.copy",
    "ansible.builtin.template",
    "ansible.builtin.file",
    "ansible.builtin.service",
    "ansible.builtin.systemd",
    "ansible.builtin.user",
    "ansible.builtin.group",
    "ansible.builtin.authorized_key",
    "ansible.builtin.archive",
    "ansible.builtin.unarchive",
    "ansible.builtin.stat",
    "ansible.builtin.debug",
    "ansible.builtin.fail",
    "ansible.builtin.setup",
    "ansible.builtin.service_facts",
    "ansible.builtin.package_facts",
    "ansible.builtin.set_fact",
    "ansible.builtin.assert",
    "ansible.builtin.command",
    "ansible.builtin.shell",
    "ansible.builtin.fetch",
    "ansible.builtin.lineinfile",
    "ansible.builtin.apk",
    "community.general.apk",
}

PARAM_PATTERNS: dict[str, re.Pattern] = {
    "package": re.compile(r"^[a-zA-Z0-9.+_*-]+$"),
    "path": re.compile(r"^/[a-zA-Z0-9_./-]+$"),
    "service": re.compile(r"^[a-zA-Z0-9_@.-]+$"),
    "state": re.compile(r"^(present|absent|latest|restarted|started|stopped|reloaded|enabled|disabled|running|directory|file|link|hard|touch)$"),
    "mode": re.compile(r"^0[0-7]{3}$"),
    "shell": re.compile(r"^/[a-zA-Z0-9_./-]+$"),
}

BANNED_PATHS: set[str] = {
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/sudoers",
    "/etc/passwd",
    "/root/.ssh",
    "/root/.ssh/authorized_keys",
}

BANNED_USERNAMES: set[str] = {
    "root",
    "admin",
    "adm",
    "daemon",
    "bin",
    "sys",
    "sync",
    "mail",
    "nobody",
}

CONTENT_MAX_LENGTH: int = 100_000
READ_ONLY_MODULES: set[str] = {
    "ansible.builtin.setup",
    "ansible.builtin.service_facts",
    "ansible.builtin.package_facts",
    "ansible.builtin.stat",
    "ansible.builtin.set_fact",
    "ansible.builtin.assert",
    "ansible.builtin.debug",
    "ansible.builtin.fail",
}

# Maps (module_name, ansible_param_name) → semantic validation rule(s)
# "content", "mode", "state" use their own name directly.
# For module-qualified params, we map to the semantic pattern key.
_PARAM_SEMANTIC_MAP: dict[tuple[str, str], str] = {
    ("ansible.builtin.package", "name"): "package",
    ("ansible.builtin.apt", "name"): "package",
    ("ansible.builtin.yum", "name"): "package",
    ("ansible.builtin.dnf", "name"): "package",
    ("ansible.builtin.copy", "dest"): "path",
    ("ansible.builtin.template", "dest"): "path",
    ("ansible.builtin.file", "path"): "path",
    ("ansible.builtin.file", "dest"): "path",
    ("ansible.builtin.service", "name"): "service",
    ("ansible.builtin.systemd", "name"): "service",
    ("ansible.builtin.user", "name"): "username",
    ("ansible.builtin.command", "cmd"): "shell_cmd",
    ("ansible.builtin.shell", "cmd"): "shell_cmd",
}


class ValidationError(Exception):
    def __init__(self, message: str, details: list[str] | None = None):
        self.details = details or []
        super().__init__(message)


def _parse_playbook(playbook_yaml: str) -> list[dict[str, Any]]:
    if not playbook_yaml or not playbook_yaml.strip():
        raise ValidationError("Empty playbook")

    try:
        data = yaml.safe_load(playbook_yaml)
    except yaml.YAMLError as e:
        raise ValidationError(f"Failed to parse YAML: {e}")

    if not isinstance(data, list):
        raise ValidationError("Playbook must be a list of plays")

    return data


def _extract_tasks(data: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    tasks: list[tuple[int, dict[str, Any]]] = []
    for play_idx, play in enumerate(data):
        play_tasks = play.get("tasks", [])
        if not isinstance(play_tasks, list):
            continue
        for task in play_tasks:
            if not isinstance(task, dict):
                continue
            tasks.append((play_idx, task))
    return tasks


def module_allowlist(
    playbook_yaml: str,
    *,
    override: bool = False,
    allow_command_modules: bool = True,
) -> list[str]:
    data = _parse_playbook(playbook_yaml)
    errors: list[str] = []

    for play_idx, task in _extract_tasks(data):
        module_name = _detect_module(task)
        if module_name is None:
            errors.append(f"Play {play_idx}: could not determine module for task {task.get('name', '?')}")
            continue

        is_allowed = module_name in ALLOWED_MODULES_TEMPLATE
        if not allow_command_modules and module_name in {"ansible.builtin.command", "ansible.builtin.shell"}:
            is_allowed = False

        if not is_allowed and not override:
            errors.append(f"Play {play_idx}: disallowed module '{module_name}' in task '{task.get('name', '?')}'")

    return errors


def _detect_module(task: dict[str, Any]) -> str | None:
    for key in task:
        if key in ("name", "when", "register", "notify", "loop", "with_items", "tags", "ignore_errors"):
            continue
        if key in ALLOWED_MODULES_TEMPLATE:
            return key
        if isinstance(task[key], dict) and key != "vars":
            return key
    return None


def _check_field(field: str, value: Any, play_idx: int, task_name: str, module_name: str) -> list[str]:
    errors: list[str] = []

    if not isinstance(value, str):
        return []

    if field == "content":
        if len(value) > CONTENT_MAX_LENGTH:
            errors.append(f"Play {play_idx}: 'content' exceeds max length ({len(value)} > {CONTENT_MAX_LENGTH}) in task '{task_name}'")
        if "\x00" in value:
            errors.append(f"Play {play_idx}: 'content' contains null bytes in task '{task_name}'")
        return errors

    semantic_key = _PARAM_SEMANTIC_MAP.get((module_name, field))

    if semantic_key == "path":
        if value in BANNED_PATHS:
            errors.append(f"Play {play_idx}: banned path '{value}' in task '{task_name}'")

    if semantic_key == "username":
        if value in BANNED_USERNAMES:
            errors.append(f"Play {play_idx}: banned username '{value}' in task '{task_name}'")

    pattern = PARAM_PATTERNS.get(semantic_key)
    if pattern and not pattern.match(value):
        errors.append(f"Play {play_idx}: invalid '{field}' value '{value}' in task '{task_name}'")

    if field == "state" and not PARAM_PATTERNS["state"].match(value):
        errors.append(f"Play {play_idx}: invalid 'state' value '{value}' in task '{task_name}'")

    return errors


def param_bounds_check(playbook_yaml: str) -> list[str]:
    data = _parse_playbook(playbook_yaml)
    errors: list[str] = []

    for play_idx, task in _extract_tasks(data):
        module_name = _detect_module(task)
        if module_name is None:
            continue

        params = task.get(module_name, {})
        if not isinstance(params, dict):
            continue

        task_name = task.get("name", "?")
        for field, value in params.items():
            errors.extend(_check_field(field, value, play_idx, task_name, module_name))

    return errors


def is_read_only_playbook(playbook_yaml: str) -> bool:
    """Return true only when every task is an explicitly read-only module."""
    try:
        data = _parse_playbook(playbook_yaml)
    except ValidationError:
        return False
    tasks = _extract_tasks(data)
    if not tasks:
        return False
    for _, task in tasks:
        module = _detect_module(task)
        if module not in READ_ONLY_MODULES:
            return False
    return True


def validate_playbook(
    playbook_yaml: str,
    *,
    override: bool = False,
    allow_command_modules: bool = True,
) -> list[str]:
    errors: list[str] = []
    errors.extend(
        module_allowlist(
            playbook_yaml,
            override=override,
            allow_command_modules=allow_command_modules,
        )
    )
    errors.extend(param_bounds_check(playbook_yaml))
    return errors
