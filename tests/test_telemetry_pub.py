from node_agent.telemetry_pub import build_payload, parse_nats_url


def test_parse_nats_url_default_port():
    assert parse_nats_url("nats://127.0.0.1") == ("127.0.0.1", 4222)


def test_build_payload_has_required_fields():
    payload = build_payload()
    assert payload["node_id"]
    assert payload["hostname"]
    assert payload["timestamp"]
    assert "ram_total_bytes" in payload
    assert "disk_total_bytes" in payload
