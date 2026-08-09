from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from orchestrator.playbook_validator import ALLOWED_MODULES_TEMPLATE, BANNED_PATHS, BANNED_USERNAMES, _detect_module

MAX_NODES_PER_TASK: int = 10
MAX_PACKAGE_NAME_LENGTH: int = 256
MAX_FILE_PATH_LENGTH: int = 4096


@dataclass
class PolicyVerdict:
    allowed: bool
    reason: str = ""
    escalate_to_human: bool = False


DANGEROUS_PATTERNS: list[re.Pattern] = [
    re.compile(r"\brm\s+-rf\s+/"),
    re.compile(r">\s*/dev/sd[a-z]"),
    re.compile(r"chmod\s+777\s+/etc"),
    re.compile(r"dd\s+if=/.+of=/dev/sd"),
    re.compile(r"mkfs\."),
    re.compile(r"mkswap\s+/dev/sd"),
    re.compile(r":(){ :\|:& };:"),
]


class PolicyGate:
    def __init__(self, max_nodes: int = MAX_NODES_PER_TASK) -> None:
        self.max_nodes = max_nodes

    def evaluate_playbook(
        self,
        playbook_yaml: str,
        target_nodes: list[str],
        task_metadata: dict[str, Any] | None = None,
    ) -> PolicyVerdict:
        metadata = task_metadata or {}
        has_human_approval = metadata.get("human_approval", False)

        for rule_name, rule_fn in self._rules():
            verdict = rule_fn(playbook_yaml, target_nodes, has_human_approval)
            if not verdict.allowed:
                return verdict

        return PolicyVerdict(allowed=True)

    def _rules(self):
        return [
            ("blast_radius", self._check_blast_radius),
            ("module_allowlist", self._check_module_allowlist),
            ("resource_bounds", self._check_resource_bounds),
            ("known_dangerous_patterns", self._check_dangerous_patterns),
            ("no_critical_nodes", self._check_critical_nodes),
        ]

    def _check_blast_radius(self, playbook_yaml: str, target_nodes: list[str], human_approval: bool) -> PolicyVerdict:
        if not human_approval and len(target_nodes) > self.max_nodes:
            return PolicyVerdict(
                allowed=False,
                reason=f"Blast radius: task targets {len(target_nodes)} nodes, exceeds max of {self.max_nodes}",
                escalate_to_human=True,
            )
        return PolicyVerdict(allowed=True)

    def _check_module_allowlist(self, playbook_yaml: str, target_nodes: list[str], human_approval: bool) -> PolicyVerdict:
        try:
            data = yaml.safe_load(playbook_yaml)
        except Exception:
            return PolicyVerdict(allowed=False, reason="Failed to parse playbook YAML")

        if not isinstance(data, list):
            return PolicyVerdict(allowed=False, reason="Playbook must be a list of plays")

        for play in data:
            for task in play.get("tasks", []):
                if not isinstance(task, dict):
                    continue
                module = _detect_module(task)
                if module and module not in ALLOWED_MODULES_TEMPLATE:
                    return PolicyVerdict(
                        allowed=False,
                        reason=f"Unauthorized module '{module}' in task '{task.get('name', '?')}'",
                    )

        return PolicyVerdict(allowed=True)

    def _check_resource_bounds(self, playbook_yaml: str, target_nodes: list[str], human_approval: bool) -> PolicyVerdict:
        try:
            data = yaml.safe_load(playbook_yaml)
        except Exception:
            return PolicyVerdict(allowed=False, reason="Failed to parse playbook YAML")

        if not isinstance(data, list):
            return PolicyVerdict(allowed=False, reason="Playbook must be a list of plays")

        for play in data:
            for task in play.get("tasks", []):
                if not isinstance(task, dict):
                    continue
                module = _detect_module(task)
                if module is None:
                    continue
                params = task.get(module, {})
                if not isinstance(params, dict):
                    continue

                for field, value in params.items():
                    if not isinstance(value, str):
                        continue

                    if field == "name":
                        if len(value) > MAX_PACKAGE_NAME_LENGTH:
                            return PolicyVerdict(
                                allowed=False,
                                reason=f"Package name exceeds {MAX_PACKAGE_NAME_LENGTH} chars in task '{task.get('name', '?')}'",
                            )

                    if field in ("dest", "path"):
                        if len(value) > MAX_FILE_PATH_LENGTH:
                            return PolicyVerdict(
                                allowed=False,
                                reason=f"File path exceeds {MAX_FILE_PATH_LENGTH} chars in task '{task.get('name', '?')}'",
                            )
                        if value in BANNED_PATHS:
                            return PolicyVerdict(
                                allowed=False,
                                reason=f"Banned path '{value}' in task '{task.get('name', '?')}'",
                            )
                        if "\x00" in value:
                            return PolicyVerdict(
                                allowed=False,
                                reason=f"Null bytes in path in task '{task.get('name', '?')}'",
                            )

                    if field == "content" and "\x00" in value:
                        return PolicyVerdict(
                            allowed=False,
                            reason=f"Null bytes in content in task '{task.get('name', '?')}'",
                        )

                    if field in ("name", "username") and value in BANNED_USERNAMES:
                        return PolicyVerdict(
                            allowed=False,
                            reason=f"Banned username '{value}' in task '{task.get('name', '?')}'",
                        )

        return PolicyVerdict(allowed=True)

    def _check_dangerous_patterns(self, playbook_yaml: str, target_nodes: list[str], human_approval: bool) -> PolicyVerdict:
        for pattern in DANGEROUS_PATTERNS:
            match = pattern.search(playbook_yaml)
            if match:
                return PolicyVerdict(
                    allowed=False,
                    reason=f"Dangerous pattern detected: '{match.group()[:50]}'",
                    escalate_to_human=True,
                )
        return PolicyVerdict(allowed=True)

    def _check_critical_nodes(self, playbook_yaml: str, target_nodes: list[str], human_approval: bool) -> PolicyVerdict:
        if human_approval:
            return PolicyVerdict(allowed=True)

        try:
            data = yaml.safe_load(playbook_yaml)
        except Exception:
            return PolicyVerdict(allowed=False, reason="Failed to parse playbook YAML for critical node check")

        if not isinstance(data, list):
            return PolicyVerdict(allowed=False, reason="Playbook must be a list of plays")

        hosts: set[str] = set()
        for play in data:
            h = play.get("hosts", "")
            if isinstance(h, str):
                hosts.add(h)

        # If the playbook targets "orchestrator" group specifically,
        # that's potentially critical
        if "orchestrator" in hosts:
            return PolicyVerdict(
                allowed=False,
                reason="Playbook targets orchestrator group (critical nodes)",
                escalate_to_human=True,
            )

        return PolicyVerdict(allowed=True)
