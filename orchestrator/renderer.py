from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from orchestrator.models import ActionType, Intent, NodeSelector

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "ansible" / "templates"

TEMPLATE_MAP = {
    ActionType.install_pkg: "install-pkg.yml.j2",
    ActionType.update_system: "update-system.yml.j2",
    ActionType.add_user: "add-user.yml.j2",
    ActionType.config_file: "config-file.yml.j2",
    ActionType.restart_service: "restart-service.yml.j2",
}


def resolve_target(target: NodeSelector) -> str:
    if target.group:
        return target.group
    return target.node_id if target.node_id else "managed_nodes"


def render_playbook(intent: Intent) -> str:
    template_name = TEMPLATE_MAP.get(intent.action)
    if not template_name:
        raise ValueError(f"no template for action: {intent.action}")

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        undefined=StrictUndefined,
    )
    template = env.get_template(template_name)

    target = resolve_target(intent.target)

    return template.render(target=target, params=intent.params)
