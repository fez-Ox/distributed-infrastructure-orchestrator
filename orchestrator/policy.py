from __future__ import annotations

from orchestrator.models import ActionType, AgentMode, Intent, TaskStatus


class PolicyViolation(Exception):
    pass


class PolicyEngine:
    def __init__(
        self,
        valid_node_ids: set[str],
        valid_groups: set[str] | None = None,
        mode: str = AgentMode.orchestrator.value,
        child_node_id: str | None = None,
        allowed_actions: set[ActionType] | None = None,
    ) -> None:
        self.valid_node_ids = valid_node_ids
        self.valid_groups = valid_groups or {"managed_nodes", "lxcs", "vms", "orchestrator"}
        self.mode = mode
        self.child_node_id = child_node_id
        if mode == AgentMode.child.value and allowed_actions is None:
            from node_agent.child_catalog import CHILD_ACTIONS
            allowed_actions = CHILD_ACTIONS
        self.allowed_actions = allowed_actions

    def validate(self, intent: Intent) -> None:
        if self.mode == AgentMode.child.value:
            self._validate_child(intent)
            return
        target = intent.target
        if not target.node_id and not target.group:
            raise PolicyViolation("target must specify node_id or group")
        if target.node_id and target.node_id not in self.valid_node_ids:
            raise PolicyViolation(f"unknown node_id: {target.node_id}")
        if target.group and target.group not in self.valid_groups:
            raise PolicyViolation(f"unknown group: {target.group}")
        self._validate_params(intent)

    def _validate_child(self, intent: Intent) -> None:
        if self.allowed_actions is not None and intent.action not in self.allowed_actions:
            raise PolicyViolation(f"action '{intent.action.value}' is not allowed on a child agent")
        target = intent.target
        if target.group and target.group != "localhost":
            raise PolicyViolation(f"child agent cannot target group '{target.group}'")
        if target.node_id and target.node_id not in (self.child_node_id, "localhost"):
            raise PolicyViolation(f"child agent can only target itself ('{self.child_node_id}'), not '{target.node_id}'")
        if not target.node_id and not target.group:
            raise PolicyViolation("target must specify node_id or group")
        from node_agent.child_catalog import validate_params
        errors = validate_params(intent.action, intent.params or {})
        if errors:
            raise PolicyViolation("; ".join(errors))

    def _validate_params(self, intent: Intent) -> None:
        action = intent.action
        p = intent.params
        if action == ActionType.install_pkg:
            if not p.get("package"):
                raise PolicyViolation("install-pkg requires 'package'")
            allowed_states = {"present", "latest", "absent"}
            if p.get("state", "present") not in allowed_states:
                raise PolicyViolation(f"invalid package state: {p.get('state')}")
        elif action == ActionType.add_user:
            if not p.get("username"):
                raise PolicyViolation("add-user requires 'username'")
        elif action == ActionType.config_file:
            if not p.get("path"):
                raise PolicyViolation("config-file requires 'path'")
            if not p.get("content"):
                raise PolicyViolation("config-file requires 'content'")
        elif action == ActionType.restart_service:
            if not p.get("service"):
                raise PolicyViolation("restart-service requires 'service'")
            allowed_states = {"restarted", "started", "stopped", "reloaded"}
            if p.get("state", "restarted") not in allowed_states:
                raise PolicyViolation(f"invalid service state: {p.get('state')}")

    def allowed_transition(self, current: TaskStatus, next_status: TaskStatus) -> bool:
        transitions = {
            TaskStatus.pending: {TaskStatus.planning},
            TaskStatus.planning: {TaskStatus.awaiting_approval, TaskStatus.failed},
            TaskStatus.awaiting_approval: {TaskStatus.running, TaskStatus.rejected},
            TaskStatus.running: {TaskStatus.verifying, TaskStatus.failed},
            TaskStatus.verifying: {TaskStatus.succeeded, TaskStatus.failed},
        }
        allowed = transitions.get(current, set())
        return next_status in allowed
