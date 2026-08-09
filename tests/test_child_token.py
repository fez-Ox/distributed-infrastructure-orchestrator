import time

import pytest

from orchestrator.child_token import sign_task, verify_token

SECRET = "s3cret-value"


class TestSignAndVerify:
    def test_roundtrip(self):
        token = sign_task(SECRET, task_id=42, node_id="lxc1")
        assert verify_token(SECRET, token, 42, "lxc1")

    def test_tampered_token_rejected(self):
        token = sign_task(SECRET, task_id=42, node_id="lxc1")
        assert not verify_token(SECRET, token + "x", 42, "lxc1")

    def test_wrong_task_id_rejected(self):
        token = sign_task(SECRET, task_id=42, node_id="lxc1")
        assert not verify_token(SECRET, token, 43, "lxc1")

    def test_wrong_node_id_rejected(self):
        token = sign_task(SECRET, task_id=42, node_id="lxc1")
        assert not verify_token(SECRET, token, 42, "lxc2")

    def test_wrong_secret_rejected(self):
        token = sign_task(SECRET, task_id=42, node_id="lxc1")
        assert not verify_token("other-secret", token, 42, "lxc1")

    def test_empty_secret_rejected(self):
        token = sign_task(SECRET, task_id=42, node_id="lxc1")
        assert not verify_token("", token, 42, "lxc1")

    def test_empty_token_rejected(self):
        assert not verify_token(SECRET, "", 42, "lxc1")

    def test_garbage_token_rejected(self):
        assert not verify_token(SECRET, "not-a-token", 42, "lxc1")


class TestExpiry:
    def test_expired_token_rejected(self, monkeypatch):
        t0 = 1_000_000.0
        monkeypatch.setattr(time, "time", lambda: t0)
        token = sign_task(SECRET, task_id=42, node_id="lxc1", ttl=5)

        monkeypatch.setattr(time, "time", lambda: t0 + 6)
        assert not verify_token(SECRET, token, 42, "lxc1")

    def test_token_valid_within_ttl(self, monkeypatch):
        t0 = 1_000_000.0
        monkeypatch.setattr(time, "time", lambda: t0)
        token = sign_task(SECRET, task_id=42, node_id="lxc1", ttl=300)

        monkeypatch.setattr(time, "time", lambda: t0 + 60)
        assert verify_token(SECRET, token, 42, "lxc1")
