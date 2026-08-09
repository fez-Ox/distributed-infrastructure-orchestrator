.PHONY: install test db-init download-nats start-nats state-manager telemetry ansible-ping setup-vm1 sync-vm1 smoke-test agent lint stop deploy-child-agents child-agent

install:
	uv sync

download-nats:
	scripts/download_nats_server.sh

start-nats:
	scripts/start_nats_local.sh

setup-vm1:
	scripts/setup_vm1_orchestrator.sh

sync-vm1:
	scripts/sync_vm1.sh

test:
	uv run pytest

db-init:
	uv run python -m orchestrator.db

state-manager:
	scripts/start_state_manager.sh

telemetry:
	uv run python -m node_agent.telemetry_pub

agent:
	uv run python -m orchestrator

child-agent:
	uv run python -m node_agent.child_agent

deploy-child-agents:
	ANSIBLE_CONFIG=ansible/ansible.cfg uv run ansible-playbook -i ansible/inventory.ini ansible/playbooks/install_child_agent.yml

stop:
	bash scripts/stop_orchestration.sh

lint:
	uv run python -m py_compile orchestrator/*.py tests/*.py

ansible-ping:
	ANSIBLE_CONFIG=ansible/ansible.cfg uv run ansible managed_nodes -i ansible/inventory.ini -m ping

smoke-test:
	bash scripts/smoke_test.sh
