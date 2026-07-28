from __future__ import annotations

import json
import os
import shutil
import socket
import time
from datetime import datetime, timezone

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
NODE_ID = os.environ.get("NODE_ID") or socket.gethostname().lower()
NODE_TYPE = os.environ.get("NODE_TYPE", "unknown")
NODE_ROLE = os.environ.get("NODE_ROLE", "worker")
INTERVAL_SECONDS = int(os.environ.get("TELEMETRY_INTERVAL_SECONDS", "5"))


def parse_nats_url(url: str) -> tuple[str, int]:
    if not url.startswith("nats://"):
        raise ValueError("NATS_URL must start with nats://")
    host_port = url.removeprefix("nats://").split("/", 1)[0]
    host, _, port = host_port.partition(":")
    return host, int(port or "4222")


def read_first_float(path: str) -> float | None:
    try:
        return float(open(path, "r", encoding="utf-8").read().strip())
    except Exception:
        return None


def read_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                key, raw = line.split(":", 1)
                values[key] = int(raw.strip().split()[0]) * 1024
    except Exception:
        pass
    return values


def read_uptime() -> int | None:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return None


def read_cpu_percent() -> float | None:
    # 1-minute load average normalized by CPU count. Cheap and good enough for MVP telemetry.
    try:
        load1, _, _ = os.getloadavg()
        cpus = os.cpu_count() or 1
        return round(min((load1 / cpus) * 100, 100.0), 2)
    except Exception:
        return None


def local_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))
            return s.getsockname()[0]
    except Exception:
        return None


def build_payload() -> dict:
    mem = read_meminfo()
    ram_total = mem.get("MemTotal")
    ram_available = mem.get("MemAvailable", mem.get("MemFree"))
    ram_used = ram_total - ram_available if ram_total is not None and ram_available is not None else None
    ram_percent = round((ram_used / ram_total) * 100, 2) if ram_total and ram_used is not None else None

    disk = shutil.disk_usage("/")
    disk_used = disk.total - disk.free

    return {
        "node_id": NODE_ID,
        "hostname": socket.gethostname(),
        "ip": local_ip(),
        "node_type": NODE_TYPE,
        "role": NODE_ROLE,
        "cpu_percent": read_cpu_percent(),
        "ram_total_bytes": ram_total,
        "ram_used_bytes": ram_used,
        "ram_percent": ram_percent,
        "disk_total_bytes": disk.total,
        "disk_used_bytes": disk_used,
        "disk_percent": round((disk_used / disk.total) * 100, 2) if disk.total else None,
        "uptime_seconds": read_uptime(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def nats_pub(subject: str, payload: dict) -> None:
    host, port = parse_nats_url(NATS_URL)
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.settimeout(5)
        sock.recv(1024)  # INFO line
        sock.sendall(b'CONNECT {"verbose":false,"pedantic":false}\r\n')
        cmd = f"PUB {subject} {len(body)}\r\n".encode("ascii") + body + b"\r\n"
        sock.sendall(cmd)


def main() -> None:
    subject = f"telemetry.{NODE_ID}.system"
    while True:
        try:
            nats_pub(subject, build_payload())
        except Exception as exc:
            print(f"telemetry publish failed: {exc}", flush=True)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
