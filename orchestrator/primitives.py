from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


class PrimitiveCall(BaseModel):
    action: str = Field(description="Primitive action name")
    params: dict[str, Any] = Field(default_factory=dict, description="Primitive parameters")


@dataclass
class EffectDescriptor:
    packages_installed: list[str] = field(default_factory=list)
    packages_removed: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    services_affected: list[str] = field(default_factory=list)
    ports_opened: list[tuple[int, str]] = field(default_factory=list)


KNOWN_PRIMITIVES: set[str] = {
    "install_package",
    "update_system",
    "ensure_service_state",
    "restart_service",
    "write_file",
    "create_user",
}


def validate_primitive(call: PrimitiveCall) -> list[str]:
    errors: list[str] = []
    if call.action not in KNOWN_PRIMITIVES:
        errors.append(f"Unknown primitive '{call.action}'")
        return errors

    if call.action == "install_package":
        pkg = call.params.get("package", "")
        if not pkg:
            errors.append("install_package: 'package' is required")
    elif call.action == "ensure_service_state":
        svc = call.params.get("service", "")
        state = call.params.get("state", "")
        if not svc:
            errors.append("ensure_service_state: 'service' is required")
        if state not in ("running", "stopped", "restarted", "reloaded"):
            errors.append(f"ensure_service_state: invalid state '{state}'")
    elif call.action == "restart_service":
        svc = call.params.get("service", "")
        if not svc:
            errors.append("restart_service: 'service' is required")
    elif call.action == "write_file":
        path = call.params.get("path", "")
        content = call.params.get("content", "")
        if not path:
            errors.append("write_file: 'path' is required")
        if not content:
            errors.append("write_file: 'content' is required")
    elif call.action == "create_user":
        username = call.params.get("username", "")
        if not username:
            errors.append("create_user: 'username' is required")
    elif call.action == "update_system":
        pass

    return errors


def expected_effect(call: PrimitiveCall) -> EffectDescriptor:
    if call.action == "install_package":
        pkg = call.params.get("package", "")
        return EffectDescriptor(packages_installed=[pkg] if pkg else [])
    elif call.action == "update_system":
        return EffectDescriptor()
    elif call.action == "ensure_service_state":
        svc = call.params.get("service", "")
        return EffectDescriptor(services_affected=[svc] if svc else [])
    elif call.action == "restart_service":
        svc = call.params.get("service", "")
        return EffectDescriptor(services_affected=[svc] if svc else [])
    elif call.action == "write_file":
        path = call.params.get("path", "")
        return EffectDescriptor(files_modified=[path] if path else [])
    elif call.action == "create_user":
        return EffectDescriptor()
    return EffectDescriptor()


def aggregate_effect(calls: list[PrimitiveCall]) -> EffectDescriptor:
    total = EffectDescriptor()
    for c in calls:
        eff = expected_effect(c)
        total.packages_installed.extend(eff.packages_installed)
        total.packages_removed.extend(eff.packages_removed)
        total.files_modified.extend(eff.files_modified)
        total.services_affected.extend(eff.services_affected)
        total.ports_opened.extend(eff.ports_opened)
    return total


def render_primitive(call: PrimitiveCall) -> list[dict[str, Any]]:
    if call.action == "install_package":
        pkg = call.params.get("package", "")
        state = call.params.get("state", "present")
        return [
            {
                "name": f"Install package {pkg}",
                "ansible.builtin.package": {
                    "name": pkg,
                    "state": state,
                },
            }
        ]
    elif call.action == "update_system":
        return [
            {
                "name": "Update all packages (apt)",
                "ansible.builtin.apt": {
                    "upgrade": "dist",
                    "update_cache": True,
                },
                "when": "ansible_pkg_mgr == 'apt'",
            },
            {
                "name": "Update all packages (apk)",
                "community.general.apk": {
                    "upgrade": "yes",
                    "update_cache": True,
                },
                "when": "ansible_facts['pkg_mgr'] == 'apk'",
            },
        ]
    elif call.action == "ensure_service_state":
        svc = call.params.get("service", "")
        state = call.params.get("state", "running")
        return [
            {
                "name": f"Ensure service {svc} is {state}",
                "ansible.builtin.service": {
                    "name": svc,
                    "state": state,
                },
            }
        ]
    elif call.action == "restart_service":
        svc = call.params.get("service", "")
        enabled = call.params.get("enabled")
        params: dict[str, Any] = {"name": svc, "state": "restarted"}
        if enabled is not None:
            params["enabled"] = enabled
        task: dict[str, Any] = {
            "name": f"Restart service {svc}",
            "ansible.builtin.service": params,
        }
        return [task]
    elif call.action == "write_file":
        path = call.params.get("path", "")
        content = call.params.get("content", "")
        mode = call.params.get("mode", "0644")
        return [
            {
                "name": f"Write file {path}",
                "ansible.builtin.copy": {
                    "dest": path,
                    "content": content,
                    "mode": mode,
                },
            }
        ]
    elif call.action == "create_user":
        username = call.params.get("username", "")
        groups = call.params.get("groups", [])
        shell = call.params.get("shell")
        ssh_key = call.params.get("ssh_key")
        tasks: list[dict[str, Any]] = [
            {
                "name": f"Create user {username}",
                "ansible.builtin.user": {
                    "name": username,
                    "state": "present",
                },
            }
        ]
        if groups:
            tasks[0]["ansible.builtin.user"]["groups"] = ",".join(groups)
        if shell:
            tasks[0]["ansible.builtin.user"]["shell"] = shell
        if ssh_key:
            tasks.append(
                {
                    "name": f"Set authorized key for {username}",
                    "ansible.builtin.authorized_key": {
                        "user": username,
                        "key": ssh_key,
                        "state": "present",
                    },
                }
            )
        return tasks

    return []


def render_primitives_to_playbook(calls: list[PrimitiveCall], target: str) -> str:
    import yaml

    tasks: list[dict[str, Any]] = []
    for c in calls:
        tasks.extend(render_primitive(c))

    play = {
        "hosts": target,
        "gather_facts": any(c.action == "update_system" for c in calls),
        "tasks": tasks,
    }

    return "---\n" + yaml.dump([play], default_flow_style=False).strip()
