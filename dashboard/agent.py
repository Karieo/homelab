#!/usr/bin/env python3
"""Homelab stats agent.

Exposes a single endpoint, ``GET /stats``, returning live hardware,
network, and container information as JSON. Runs on every node
(bastion, scout, ...) and is consumed by the dashboard at bastion:9091.

The dashboard is served on a different port than this agent, so browser
requests are cross-origin. CORS headers are emitted on every response to
make that work without a reverse proxy.
"""

import datetime
import re
import socket
import subprocess

import psutil
from flask import Flask, jsonify

app = Flask(__name__)

# Listening port for the agent. Keep in sync with the systemd unit and
# the dashboard's ENDPOINTS config.
PORT = 9090

# Friendly role labels per host. Falls back to a generic label when the
# hostname is not known.
ROLES = {
    "bastion": "Home Base — Jetson TX2",
    "scout": "Edge Node — Raspberry Pi 4B",
}

# Fallback host port for well-known services when it cannot be parsed
# from `docker ps`. Used to build click-through links on the dashboard.
KNOWN_PORTS = {
    "remndrs": 3000,
    "homer": 8888,
    "vaultwarden": 8080,
    "gitea": 3001,
    "jellyfin": 8096,
    "open-webui": 3002,
    "ollama": 11434,
    "pihole": 80,
    "kiwix": 8080,
}

# Matches the published host port in `docker ps` Ports output, e.g.
# "0.0.0.0:3000->3000/tcp" or ":::8096->8096/tcp".
_PORT_RE = re.compile(r":(\d+)->")


@app.after_request
def add_cors_headers(response):
    """Allow the dashboard (served from another origin) to fetch us."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return response


def get_cpu_temp():
    """Return CPU temperature in Celsius, or None if unavailable."""
    try:
        temps = psutil.sensors_temperatures()
        for key in ("cpu_thermal", "coretemp", "k10temp", "cpu-thermal"):
            if key in temps and temps[key]:
                return round(temps[key][0].current, 1)
    except Exception:
        pass
    # Jetson / generic sysfs fallback.
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read()) / 1000, 1)
    except Exception:
        return None


def _parse_port(ports_field):
    """Pull the first published host port out of a `docker ps` Ports cell."""
    match = _PORT_RE.search(ports_field or "")
    if match:
        return int(match.group(1))
    return None


def get_containers():
    """Return a list of running/stopped containers with their host port."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []

    containers = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        name = parts[0]
        status = parts[1] if len(parts) > 1 else ""
        ports_field = parts[2] if len(parts) > 2 else ""
        port = _parse_port(ports_field) or KNOWN_PORTS.get(name)
        containers.append(
            {
                "name": name,
                "status": "running" if status.startswith("Up") else "stopped",
                "port": port,
            }
        )
    return sorted(containers, key=lambda c: c["name"])


def get_tailscale_status():
    """Return the node's Tailscale IP and connection state."""
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        ip = result.stdout.strip().split("\n")[0]
        if ip:
            return {"tailscale_ip": ip, "tailscale_status": "connected"}
    except Exception:
        pass
    return {"tailscale_ip": None, "tailscale_status": "disconnected"}


def get_uptime():
    """Return a compact human-readable uptime string, e.g. "2d 4h 12m"."""
    uptime_seconds = (
        datetime.datetime.now()
        - datetime.datetime.fromtimestamp(psutil.boot_time())
    ).total_seconds()
    days = int(uptime_seconds // 86400)
    hours = int((uptime_seconds % 86400) // 3600)
    minutes = int((uptime_seconds % 3600) // 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


@app.route("/stats")
def stats():
    hostname = socket.gethostname()
    disk = psutil.disk_usage("/")
    ram = psutil.virtual_memory()
    return jsonify(
        {
            "hostname": hostname,
            "role": ROLES.get(hostname, "Node"),
            "uptime": get_uptime(),
            "cpu": {
                "percent": psutil.cpu_percent(interval=1),
                "temp_celsius": get_cpu_temp(),
                "cores": psutil.cpu_count(),
            },
            "ram": {
                "used_gb": round(ram.used / 1e9, 1),
                "total_gb": round(ram.total / 1e9, 1),
                "percent": ram.percent,
            },
            "disk": {
                "used_gb": round(disk.used / 1e9, 1),
                "total_gb": round(disk.total / 1e9, 1),
                "percent": disk.percent,
            },
            "network": get_tailscale_status(),
            "containers": get_containers(),
            "timestamp": datetime.datetime.now().isoformat(),
        }
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
