from orchestrator.db import claim_child_task, connect, finish_child_task, init_db, upsert_telemetry


def test_upsert_telemetry_creates_node_and_latest_row(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect(str(db_path))
    init_db(conn)

    payload = {
        "node_id": "lxc1",
        "hostname": "lxc1",
        "ip": "192.168.122.238",
        "node_type": "lxc",
        "role": "worker",
        "cpu_percent": 1.5,
        "ram_total_bytes": 100,
        "ram_used_bytes": 50,
        "ram_percent": 50.0,
        "disk_total_bytes": 1000,
        "disk_used_bytes": 100,
        "disk_percent": 10.0,
        "uptime_seconds": 123,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }

    upsert_telemetry(conn, payload)

    node = conn.execute("SELECT * FROM nodes WHERE node_id = ?", ("lxc1",)).fetchone()
    telemetry = conn.execute("SELECT * FROM telemetry_latest WHERE node_id = ?", ("lxc1",)).fetchone()

    assert node["hostname"] == "lxc1"
    assert node["enabled"] == 0
    assert telemetry["ram_percent"] == 50.0


def test_child_task_claim_is_idempotent(tmp_path):
    conn = connect(str(tmp_path / "state.db"))
    init_db(conn)

    assert claim_child_task(conn, 10, "lxc1", "key-1") == (True, None)
    assert claim_child_task(conn, 10, "lxc1", "key-1")[0] is False

    finish_child_task(conn, 10, "lxc1", "succeeded", '{"ok":true}')
    claimed, result = claim_child_task(conn, 10, "lxc1", "key-1")
    assert claimed is False
    assert result == '{"ok":true}'


def test_mark_stale_agents(tmp_path):
    from orchestrator.db import mark_stale_agents

    conn = connect(str(tmp_path / "state.db"))
    init_db(conn)
    conn.execute("INSERT INTO nodes (node_id, agent_status, agent_last_heartbeat) VALUES ('lxc1', 'online', '2020-01-01T00:00:00+00:00')")
    conn.commit()
    assert mark_stale_agents(conn, "2021-01-01T00:00:00+00:00") == 1
    assert conn.execute("SELECT agent_status FROM nodes WHERE node_id = 'lxc1'").fetchone()[0] == "stale"


def test_protocol_message_replay_is_rejected(tmp_path):
    from orchestrator.db import record_child_message

    conn = connect(str(tmp_path / "state.db"))
    init_db(conn)
    assert record_child_message(conn, "msg-1", "lxc1", "heartbeat") is True
    assert record_child_message(conn, "msg-1", "lxc1", "heartbeat") is False
