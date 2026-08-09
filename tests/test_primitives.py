from __future__ import annotations

from orchestrator.primitives import (
    EffectDescriptor,
    PrimitiveCall,
    aggregate_effect,
    expected_effect,
    render_primitive,
    render_primitives_to_playbook,
    validate_primitive,
)


class TestPrimitiveRender:
    def test_install_package_renders_correct_yaml(self):
        call = PrimitiveCall(action="install_package", params={"package": "htop", "state": "present"})
        tasks = render_primitive(call)
        assert len(tasks) == 1
        assert "ansible.builtin.package" in tasks[0]
        assert tasks[0]["ansible.builtin.package"]["name"] == "htop"
        assert tasks[0]["ansible.builtin.package"]["state"] == "present"

    def test_ensure_service_state_renders_tasks(self):
        call = PrimitiveCall(action="ensure_service_state", params={"service": "nginx", "state": "running"})
        tasks = render_primitive(call)
        assert len(tasks) == 1
        assert "ansible.builtin.service" in tasks[0]
        assert tasks[0]["ansible.builtin.service"]["name"] == "nginx"

    def test_write_file_includes_content_and_mode(self):
        call = PrimitiveCall(action="write_file", params={"path": "/etc/test.conf", "content": "data", "mode": "0644"})
        tasks = render_primitive(call)
        assert len(tasks) == 1
        assert "ansible.builtin.copy" in tasks[0]
        assert tasks[0]["ansible.builtin.copy"]["dest"] == "/etc/test.conf"
        assert tasks[0]["ansible.builtin.copy"]["content"] == "data"
        assert tasks[0]["ansible.builtin.copy"]["mode"] == "0644"

    def test_create_user_includes_ssh_key_when_given(self):
        call = PrimitiveCall(action="create_user", params={"username": "deploy", "ssh_key": "ssh-ed25519 AAAA..."})
        tasks = render_primitive(call)
        assert len(tasks) == 2
        assert tasks[0]["ansible.builtin.user"]["name"] == "deploy"
        assert tasks[1]["ansible.builtin.authorized_key"]["user"] == "deploy"

    def test_update_system_renders_update_commands(self):
        call = PrimitiveCall(action="update_system", params={})
        tasks = render_primitive(call)
        assert len(tasks) == 2
        assert any("apt" in str(t) for t in tasks)
        assert any("apk" in str(t) for t in tasks)

    def test_restart_service_sets_enabled(self):
        call = PrimitiveCall(action="restart_service", params={"service": "nginx", "enabled": True})
        tasks = render_primitive(call)
        assert len(tasks) == 1
        assert tasks[0]["ansible.builtin.service"]["enabled"] is True


class TestPrimitiveValidation:
    def test_invalid_package_name_rejected(self):
        call = PrimitiveCall(action="install_package", params={})
        errors = validate_primitive(call)
        assert len(errors) >= 1
        assert "package" in errors[0]

    def test_invalid_service_state_rejected(self):
        call = PrimitiveCall(action="ensure_service_state", params={"service": "nginx", "state": "invalid"})
        errors = validate_primitive(call)
        assert len(errors) >= 1
        assert "state" in errors[0]

    def test_missing_required_param_rejected(self):
        call = PrimitiveCall(action="write_file", params={})
        errors = validate_primitive(call)
        assert len(errors) >= 2
        assert all("required" in e for e in errors)

    def test_unknown_primitive_rejected(self):
        call = PrimitiveCall(action="delete_everything", params={})
        errors = validate_primitive(call)
        assert len(errors) >= 1
        assert "Unknown" in errors[0]


class TestEffectDescriptors:
    def test_install_package_effect_includes_package(self):
        call = PrimitiveCall(action="install_package", params={"package": "nginx"})
        eff = expected_effect(call)
        assert "nginx" in eff.packages_installed

    def test_write_file_effect_includes_path(self):
        call = PrimitiveCall(action="write_file", params={"path": "/etc/nginx.conf", "content": "data"})
        eff = expected_effect(call)
        assert "/etc/nginx.conf" in eff.files_modified

    def test_aggregate_effect_is_union(self):
        calls = [
            PrimitiveCall(action="install_package", params={"package": "nginx"}),
            PrimitiveCall(action="write_file", params={"path": "/etc/nginx.conf", "content": "data"}),
            PrimitiveCall(action="ensure_service_state", params={"service": "nginx", "state": "running"}),
        ]
        agg = aggregate_effect(calls)
        assert "nginx" in agg.packages_installed
        assert "/etc/nginx.conf" in agg.files_modified
        assert "nginx" in agg.services_affected

    def test_empty_primitive_list_has_no_effect(self):
        agg = aggregate_effect([])
        assert agg.packages_installed == []
        assert agg.files_modified == []
        assert agg.services_affected == []


class TestRenderer:
    def test_single_primitive_renders_to_playbook(self):
        calls = [PrimitiveCall(action="install_package", params={"package": "htop", "state": "present"})]
        yaml = render_primitives_to_playbook(calls, "lxc1")
        assert "hosts: lxc1" in yaml
        assert "ansible.builtin.package" in yaml
        assert "htop" in yaml

    def test_multi_primitive_playbook_has_all_tasks(self):
        calls = [
            PrimitiveCall(action="install_package", params={"package": "nginx"}),
            PrimitiveCall(action="ensure_service_state", params={"service": "nginx", "state": "running"}),
        ]
        yaml = render_primitives_to_playbook(calls, "managed_nodes")
        assert yaml.count("ansible.builtin.package") == 1
        assert yaml.count("ansible.builtin.service") == 1

    def test_group_target_resolves_correctly(self):
        calls = [PrimitiveCall(action="update_system", params={})]
        yaml = render_primitives_to_playbook(calls, "lxcs")
        assert "hosts: lxcs" in yaml

    def test_syntax_check_passes_on_rendered_playbook(self):
        import os
        import subprocess
        from pathlib import Path

        calls = [PrimitiveCall(action="install_package", params={"package": "htop", "state": "present"})]
        yaml = render_primitives_to_playbook(calls, "managed_nodes")
        tmp = Path("/tmp/test_primitive_syntax.yml")
        tmp.write_text(yaml, encoding="utf-8")
        try:
            env = os.environ.copy()
            env["PATH"] = f"{(Path(__file__).resolve().parent.parent / '.venv' / 'bin')}:{env.get('PATH', '')}"
            result = subprocess.run(
                ["ansible-playbook", "--syntax-check", "-i", "ansible/inventory.ini", str(tmp)],
                capture_output=True, text=True, timeout=30, env=env,
            )
            assert result.returncode == 0
        finally:
            tmp.unlink(missing_ok=True)
