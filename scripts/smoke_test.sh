#!/usr/bin/env bash
set -euo pipefail

uv run pytest
uv run python -m orchestrator.db
ANSIBLE_CONFIG=ansible/ansible.cfg uv run ansible-inventory -i ansible/inventory.ini --list >/dev/null
ANSIBLE_CONFIG=ansible/ansible.cfg uv run ansible managed_nodes -i ansible/inventory.ini -m ping

echo "smoke test passed"
