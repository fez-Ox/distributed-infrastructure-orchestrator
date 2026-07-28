from orchestrator.db import connect, init_db, upsert_telemetry


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
