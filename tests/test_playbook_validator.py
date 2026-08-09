from __future__ import annotations

import pytest

from orchestrator.playbook_validator import (
    is_read_only_playbook,
    module_allowlist,
    param_bounds_check,
    validate_playbook,
    ValidationError,
)

INSTALL_HTOP = """\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Install htop
      ansible.builtin.package:
        name: htop
        state: present
"""

SHELL_COMMAND = """\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Run docker container
      ansible.builtin.docker_container:
        name: test
        image: nginx
"""

MIXED_PLAYBOOK = """\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Install htop
      ansible.builtin.package:
        name: htop
        state: present
    - name: Run docker
      ansible.builtin.docker_container:
        name: test
        image: nginx
"""

EMPTY_PLAYBOOK = ""

MALFORMED_YAML = "---\n  - invalid yaml: ["

MALICIOUS_PACKAGE = """\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Install with injection
      ansible.builtin.package:
        name: "htop; rm -rf /"
        state: present
"""

SHADOW_PATH = """\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Write shadow
      ansible.builtin.copy:
        dest: /etc/shadow
        content: "hacked"
        mode: "0600"
"""

CONTENT_NULL_BYTES = """\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Bad content
      ansible.builtin.copy:
        dest: /etc/test
        content: "bad\\x00stuff"
        mode: "0644"
"""

BANNED_USERNAME = """\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Create root user
      ansible.builtin.user:
        name: root
        state: present
"""

SERVICE_WITH_SLASH = """\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Bad service
      ansible.builtin.service:
        name: "../../etc/passwd"
        state: restarted
"""

VALID_SERVICE = """\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Restart nginx
      ansible.builtin.service:
        name: nginx
        state: restarted
"""

OVERRIDE_DOCKER = """\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Docker with override
      ansible.builtin.docker_container:
        name: test
        image: nginx
"""


def test_read_only_playbook_classification():
    assert is_read_only_playbook("""
---
- hosts: managed_nodes
  tasks:
    - ansible.builtin.setup:
    - ansible.builtin.debug:
        msg: ok
""")
    assert not is_read_only_playbook(INSTALL_HTOP)


class TestModuleAllowlist:
    def test_allowed_module_passes(self):
        assert module_allowlist(INSTALL_HTOP) == []

    def test_disallowed_module_fails(self):
        errors = module_allowlist(SHELL_COMMAND)
        assert len(errors) >= 1
        assert "disallowed module" in errors[0]
        assert "ansible.builtin.docker_container" in errors[0]

    def test_override_flag_allows(self):
        errors = module_allowlist(OVERRIDE_DOCKER, override=True)
        assert errors == []

    def test_playbook_with_mixed_modules(self):
        errors = module_allowlist(MIXED_PLAYBOOK)
        assert len(errors) == 1
        assert "ansible.builtin.docker_container" in errors[0]

    def test_empty_playbook_rejected(self):
        with pytest.raises(ValidationError, match="Empty playbook"):
            module_allowlist(EMPTY_PLAYBOOK)

    def test_malformed_yaml_rejected(self):
        with pytest.raises(ValidationError, match="Failed to parse YAML"):
            module_allowlist(MALFORMED_YAML)


class TestParamBounds:
    def test_safe_package_name_passes(self):
        assert param_bounds_check(INSTALL_HTOP) == []

    def test_malicious_package_name_rejected(self):
        errors = param_bounds_check(MALICIOUS_PACKAGE)
        assert len(errors) >= 1
        assert "invalid" in errors[0].lower() or "package" in errors[0]

    def test_absolute_path_allowed(self):
        playbook = """\
---
- hosts: lxc1
  gather_facts: false
  tasks:
    - name: Write config
      ansible.builtin.copy:
        dest: /etc/nginx/nginx.conf
        content: "server {}"
        mode: "0644"
"""
        assert param_bounds_check(playbook) == []

    def test_shadow_path_rejected(self):
        errors = param_bounds_check(SHADOW_PATH)
        assert len(errors) >= 1
        assert "/etc/shadow" in errors[0]

    def test_null_bytes_in_content_rejected(self):
        errors = param_bounds_check(CONTENT_NULL_BYTES)
        assert len(errors) >= 1
        assert "null bytes" in errors[0].lower()

    def test_banned_username_rejected(self):
        errors = param_bounds_check(BANNED_USERNAME)
        assert len(errors) >= 1
        assert "root" in errors[0]

    def test_valid_service_name_passes(self):
        assert param_bounds_check(VALID_SERVICE) == []

    def test_invalid_service_name_with_slash_rejected(self):
        errors = param_bounds_check(SERVICE_WITH_SLASH)
        assert len(errors) >= 1
        assert "invalid" in errors[0].lower() or "service" in errors[0]


class TestFullValidationPipeline:
    def test_full_validation_pipeline(self):
        errors = validate_playbook(INSTALL_HTOP)
        assert errors == []

    def test_full_validation_fails_disallowed(self):
        errors = validate_playbook(SHELL_COMMAND)
        assert len(errors) >= 1
        assert "disallowed module" in errors[0]

    def test_full_validation_fails_param(self):
        errors = validate_playbook(MALICIOUS_PACKAGE)
        assert len(errors) >= 1
