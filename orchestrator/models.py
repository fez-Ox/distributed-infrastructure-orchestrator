from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field
from typing_extensions import TypedDict


class AgentMode(str, Enum):
    orchestrator = "orchestrator"
    child = "child"


class TaskType(str, Enum):
    template = "template"
    freeform = "freeform"
    child = "child"


class ActionType(str, Enum):
    install_pkg = "install-pkg"
    update_system = "update-system"
    add_user = "add-user"
    config_file = "config-file"
    restart_service = "restart-service"
    freeform = "freeform"
    service_status = "service-status"
    log_tail = "log-tail"
    disk_check = "disk-check"
    workload_readiness = "workload-readiness"
    capabilities_report = "capabilities-report"
    install_local_package = "install-local-package"
    ensure_local_service = "ensure-local-service"
    write_local_state = "write-local-state"


class TaskStatus(str, Enum):
    pending = "pending"
    planning = "planning"
    awaiting_approval = "awaiting_approval"
    running = "running"
    verifying = "verifying"
    succeeded = "succeeded"
    failed = "failed"
    rejected = "rejected"
    rolling_back = "rolling_back"
    rolled_back = "rolled_back"
    rollback_failed = "rollback_failed"


class NodeSelector(BaseModel):
    node_id: str | None = Field(None, description="Single target node_id")
    group: str | None = Field(None, description="Ansible group name (e.g. managed_nodes, lxcs, vms)")


class InstallPkgParams(BaseModel):
    package: str
    state: str = "present"


class UpdateSystemParams(BaseModel):
    pass


class AddUserParams(BaseModel):
    username: str
    groups: list[str] = Field(default_factory=list)
    shell: str | None = None
    ssh_key: str | None = None


class ConfigFileParams(BaseModel):
    path: str
    content: str
    mode: str = "0644"


class RestartServiceParams(BaseModel):
    service: str
    state: str = "restarted"
    enabled: bool | None = None


class PlanStatus(str, Enum):
    planning = "planning"
    awaiting_approval = "awaiting_approval"
    executing = "executing"
    completed = "completed"
    failed = "failed"
    rolling_back = "rolling_back"
    rolled_back = "rolled_back"
    rollback_failed = "rollback_failed"


class StepStatus(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"
    rolling_back = "rolling_back"
    rolled_back = "rolled_back"
    rollback_failed = "rollback_failed"


class WorkflowStep(BaseModel):
    description: str
    playbook: str
    rollback_playbook: str | None = Field(None, description="Ansible playbook YAML to undo this step")
    verification: str | None = None


class Plan(BaseModel):
    description: str
    steps: list[WorkflowStep]


class Intent(BaseModel):
    action: ActionType
    target: NodeSelector
    params: dict[str, Any]
    reason: str = Field(..., description="Human-readable explanation of the intended change")

    def params_model(self) -> BaseModel | None:
        mapping = {
            ActionType.install_pkg: InstallPkgParams,
            ActionType.update_system: UpdateSystemParams,
            ActionType.add_user: AddUserParams,
            ActionType.config_file: ConfigFileParams,
            ActionType.restart_service: RestartServiceParams,
        }
        model_cls = mapping.get(self.action)
        if model_cls is None:
            return None
        return model_cls(**self.params)


class AgentState(TypedDict):
    user_input: str
    topology_context: str
    freeform_mode: bool
    intent: Any
    task_id: Optional[int]
    task_target: str
    playbook_path: Optional[str]
    policy_ok: bool
    syntax_ok: bool
    plan: Any
    plan_id: Optional[int]
    current_step_index: int
    ansible_stdout: str
    ansible_stderr: str
    error: Optional[str]
    status: str
    authorization_ok: bool
