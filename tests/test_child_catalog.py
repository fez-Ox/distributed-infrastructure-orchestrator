from orchestrator.models import ActionType, Intent, NodeSelector
from node_agent.child_catalog import (
    CHILD_ACTIONS,
    ALLOWED_LOG_DIRS,
    validate_params,
    render_child_playbook,
    render_child_tasks,
)


def _intent(action, params=None):
    return Intent(
        action=action,
        target=NodeSelector(node_id="lxc1"),
        params=params or {},
        reason="test",
    )


class TestChildActionSet:
    def test_mutating_actions_excluded(self):
        assert ActionType.install_pkg not in CHILD_ACTIONS
        assert ActionType.update_system not in CHILD_ACTIONS
        assert ActionType.restart_service not in CHILD_ACTIONS

    def test_inspection_and_bounded_remediation_actions_included(self):
        for action in (
            ActionType.service_status,
            ActionType.log_tail,
            ActionType.disk_check,
            ActionType.workload_readiness,
            ActionType.capabilities_report,
            ActionType.install_local_package,
            ActionType.ensure_local_service,
            ActionType.write_local_state,
        ):
            assert action in CHILD_ACTIONS


class TestValidateParams:
    def test_service_status_valid(self):
        assert validate_params(ActionType.service_status, {"service": "nginx"}) == []

    def test_service_status_missing_service(self):
        assert validate_params(ActionType.service_status, {}) != []

    def test_service_status_invalid_name(self):
        assert validate_params(ActionType.service_status, {"service": "../evil"}) != []

    def test_log_tail_bad_path_rejected(self):
        errors = validate_params(ActionType.log_tail, {"path": "/etc/shadow"})
        assert any("allowed" in e for e in errors)

    def test_log_tail_valid_path(self):
        assert (
            validate_params(
                ActionType.log_tail,
                {"path": "/var/log/messages", "lines": 20},
            )
            == []
        )

    def test_log_tail_required_path(self):
        assert validate_params(ActionType.log_tail, {}) != []

    def test_log_tail_lines_out_of_range(self):
        errors = validate_params(
            ActionType.log_tail,
            {"path": "/var/log/messages", "lines": 9999},
        )
        assert any("lines" in e for e in errors)

    def test_allowed_log_dirs(self):
        assert "/var/log/" in ALLOWED_LOG_DIRS
        assert "/opt/mao/logs/" in ALLOWED_LOG_DIRS

    def test_local_package_validation(self):
        assert validate_params(ActionType.install_local_package, {"package": "htop"}) == []
        assert validate_params(ActionType.install_local_package, {"package": "htop; rm -rf /"})

    def test_local_service_validation(self):
        assert validate_params(ActionType.ensure_local_service, {"service": "nginx", "state": "started"}) == []
        assert validate_params(ActionType.ensure_local_service, {"service": "nginx", "state": "running"})

    def test_local_state_cannot_escape_directory(self):
        assert validate_params(ActionType.write_local_state, {"name": "../passwd", "content": "x"})
        assert validate_params(ActionType.write_local_state, {"name": "status.json", "content": "x"}) == []


class TestRenderChildPlaybook:
    def test_render_hosts_localhost(self):
        playbook = render_child_playbook(_intent(ActionType.service_status, {"service": "nginx"}))
        assert "hosts: localhost" in playbook
        assert "nginx" in playbook

    def test_render_read_only_command_tasks(self):
        tasks = render_child_tasks(_intent(ActionType.service_status, {"service": "sshd"}))
        assert tasks[0]["ansible.builtin.command"]["cmd"] == "systemctl is-active sshd"
        assert tasks[0]["changed_when"] is False

    def test_render_disk_check(self):
        tasks = render_child_tasks(_intent(ActionType.disk_check))
        assert any("df -h /" in t["ansible.builtin.command"]["cmd"] for t in tasks)

    def test_render_mutation_uses_declarative_modules(self):
        tasks = render_child_tasks(_intent(ActionType.install_local_package, {"package": "htop"}))
        assert "ansible.builtin.package" in tasks[0]
        assert "ansible.builtin.shell" not in tasks[0]

        tasks = render_child_tasks(_intent(ActionType.write_local_state, {"name": "status", "content": "ok"}))
        assert any("ansible.builtin.copy" in task for task in tasks)
        assert all("ansible.builtin.shell" not in task for task in tasks)

    def test_render_unknown_action_raises(self):
        intent = _intent(ActionType.install_pkg, {"package": "htop"})
        try:
            render_child_tasks(intent)
        except ValueError:
            return
        raise AssertionError("expected ValueError for non-child action")
