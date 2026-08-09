import json

from scripts.render_nats_config import render


def test_nats_config_uses_per_node_subject_permissions(monkeypatch):
    monkeypatch.setenv("NATS_ORCHESTRATOR_USER", "orchestrator")
    monkeypatch.setenv("NATS_ORCHESTRATOR_PASSWORD", "secret")
    monkeypatch.setenv(
        "NATS_CHILD_CREDENTIALS_JSON",
        json.dumps({"lxc1": {"user": "child-lxc1", "password": "pw1"}}),
    )
    monkeypatch.setenv(
        "NATS_TELEMETRY_CREDENTIALS_JSON",
        json.dumps({"lxc1": {"user": "telemetry-lxc1", "password": "telemetry-pw1"}}),
    )

    config = render()
    assert 'user: "orchestrator"' in config
    assert 'publish: ["agent.lxc1.heartbeat"' in config
    assert 'subscribe: ["agent.lxc1.tasks.request"' in config
    assert 'publish: ["telemetry.lxc1.system"]' in config
    assert 'publish: ["agent.*.tasks.request"' in config
    assert "agent.>." not in config


def test_nats_config_rejects_wildcard_node_id(monkeypatch):
    monkeypatch.setenv("NATS_ORCHESTRATOR_USER", "orchestrator")
    monkeypatch.setenv("NATS_ORCHESTRATOR_PASSWORD", "secret")
    monkeypatch.setenv(
        "NATS_CHILD_CREDENTIALS_JSON",
        json.dumps({"lxc.*": {"user": "child", "password": "pw"}}),
    )

    try:
        render()
    except ValueError as exc:
        assert "invalid node ID" in str(exc)
    else:
        raise AssertionError("wildcard node ID should be rejected")
