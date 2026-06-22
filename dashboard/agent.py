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
import json
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import psutil
from flask import Flask, jsonify, request

app = Flask(__name__)

# Listening port for the agent. Keep in sync with the systemd unit and
# the dashboard's ENDPOINTS config.
PORT = 9090

# SQLite database for historical samples (CPU/temp/RAM/disk over time).
# Lives next to the agent so the service user can write it.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.db")

# How long to keep history, and the minimum spacing between recorded
# samples (the dashboard polls every 30s, but guard against bursts).
HISTORY_RETENTION_DAYS = 7
MIN_SAMPLE_INTERVAL_SEC = 20

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

# Native (non-Docker) services to report per host, checked via
# `systemctl is-active`. Some nodes run things like Pi-hole or Kiwix as
# plain systemd services rather than containers; list them here so they
# show up alongside Docker containers on the dashboard. `port` builds the
# click-through link (set to None for no link).
EXTRA_SERVICES = {
    "scout": [
        {"name": "pihole", "unit": "pihole-FTL.service", "port": 80},
        {"name": "kiwix", "unit": "kiwix.service", "port": 8080},
    ],
}

# Pi-hole integration, per host. The agent runs on the same box as
# Pi-hole, so it queries the local admin API. Supports both Pi-hole v6
# (REST API at /api) and v5 (api.php), auto-detected.
#
# Provide the web/app password via the PIHOLE_PASSWORD env var (set it in
# the systemd unit) rather than committing a secret here. Leave a host out
# of this map to disable the widget for it.
PIHOLE = {
    "scout": {
        "base_url": os.environ.get("PIHOLE_BASE_URL", "http://localhost"),
        "password": os.environ.get("PIHOLE_PASSWORD", ""),
    },
}

# Matches the published host port in `docker ps` Ports output, e.g.
# "0.0.0.0:3000->3000/tcp" or ":::8096->8096/tcp".
_PORT_RE = re.compile(r":(\d+)->")

# Cached Pi-hole v6 session id, reused across requests until it expires.
_pihole_sid = None
_pihole_lock = threading.Lock()

# Last network counter sample, for computing throughput between polls.
_net_lock = threading.Lock()
_net_last = {"ts": None, "rx": 0, "tx": 0}


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
    return containers


def _systemd_active(unit):
    """True if a systemd unit is active. Read-only query, no root needed."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() == "active"
    except Exception:
        return False


def get_extra_services(hostname):
    """Report configured native systemd services for this host."""
    services = []
    for svc in EXTRA_SERVICES.get(hostname, []):
        services.append(
            {
                "name": svc["name"],
                "status": "running" if _systemd_active(svc["unit"]) else "stopped",
                "port": svc.get("port"),
            }
        )
    return services


def get_services(hostname):
    """Combined, de-duplicated list of Docker containers + native services."""
    services = get_containers()
    seen = {s["name"] for s in services}
    for svc in get_extra_services(hostname):
        if svc["name"] not in seen:
            services.append(svc)
    return sorted(services, key=lambda s: s["name"])


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


def get_network_throughput():
    """Bytes/sec in/out across physical interfaces, averaged since the last
    call. Returns zeros on the first sample. Virtual/overlay interfaces are
    skipped to avoid double-counting traffic that also crosses eth0/wlan0."""
    counters = psutil.net_io_counters(pernic=True)
    rx = tx = 0
    for nic, c in counters.items():
        if nic == "lo" or nic.startswith(
            ("docker", "veth", "br-", "tailscale", "virbr")
        ):
            continue
        rx += c.bytes_recv
        tx += c.bytes_sent

    now = time.time()
    with _net_lock:
        last = dict(_net_last)
        _net_last.update({"ts": now, "rx": rx, "tx": tx})

    if last["ts"] is None or now <= last["ts"]:
        return {"rx_bytes_sec": 0, "tx_bytes_sec": 0}
    dt = now - last["ts"]
    return {
        "rx_bytes_sec": max(0, round((rx - last["rx"]) / dt)),
        "tx_bytes_sec": max(0, round((tx - last["tx"]) / dt)),
    }


# ---- Historical samples (SQLite) --------------------------------------

def init_db():
    """Create the history table if it doesn't exist."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS samples ("
            "ts INTEGER PRIMARY KEY, cpu REAL, temp REAL, ram REAL, disk REAL)"
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def record_sample(cpu, temp, ram, disk):
    """Append a sample (throttled) and prune rows past the retention window."""
    now = int(time.time())
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        last = conn.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
        if last and now - last < MIN_SAMPLE_INTERVAL_SEC:
            conn.close()
            return
        conn.execute(
            "INSERT OR REPLACE INTO samples (ts, cpu, temp, ram, disk) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, cpu, temp, ram, disk),
        )
        conn.execute(
            "DELETE FROM samples WHERE ts < ?",
            (now - HISTORY_RETENTION_DAYS * 86400,),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_history(hours, max_points=120):
    """Return downsampled history for the last `hours` as parallel arrays."""
    empty = {"ts": [], "cpu": [], "temp": [], "ram": [], "disk": []}
    since = int(time.time()) - hours * 3600
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        rows = conn.execute(
            "SELECT ts, cpu, temp, ram, disk FROM samples "
            "WHERE ts >= ? ORDER BY ts",
            (since,),
        ).fetchall()
        conn.close()
    except Exception:
        return empty
    if len(rows) > max_points:
        stride = len(rows) // max_points + 1
        rows = rows[::stride]
    return {
        "ts": [r[0] for r in rows],
        "cpu": [r[1] for r in rows],
        "temp": [r[2] for r in rows],
        "ram": [r[3] for r in rows],
        "disk": [r[4] for r in rows],
    }


# ---- Pi-hole integration ----------------------------------------------

def _http_json(url, data=None, headers=None, timeout=3):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _pihole_v6_auth(base, password):
    """Authenticate to Pi-hole v6 and cache the session id."""
    global _pihole_sid
    payload = json.dumps({"password": password}).encode()
    res = _http_json(
        base + "/api/auth",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    _pihole_sid = res.get("session", {}).get("sid")
    return _pihole_sid


def _pihole_v6(base, password):
    """Pi-hole v6 REST API. Reuses a cached SID, re-auths on expiry."""
    global _pihole_sid
    for _ in range(2):
        sid = _pihole_sid or _pihole_v6_auth(base, password)
        if not sid:
            return None
        try:
            res = _http_json(
                base + "/api/stats/summary?sid=" + urllib.parse.quote(sid)
            )
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                _pihole_sid = None
                continue
            raise
        q = res.get("queries", {})
        return {
            "queries_today": q.get("total"),
            "blocked_today": q.get("blocked"),
            "blocked_percent": round(q.get("percent_blocked", 0) or 0, 1),
        }
    return None


def _pihole_v5(base, password):
    """Legacy Pi-hole v5 api.php fallback."""
    url = base + "/admin/api.php?summaryRaw"
    if password:
        url += "&auth=" + urllib.parse.quote(password)
    res = _http_json(url)
    if not res or "dns_queries_today" not in res:
        return None
    return {
        "queries_today": res.get("dns_queries_today"),
        "blocked_today": res.get("ads_blocked_today"),
        "blocked_percent": round(float(res.get("ads_percentage_today", 0)), 1),
    }


def get_pihole_stats(hostname):
    """Return Pi-hole query/blocking summary, or None if not configured/up."""
    cfg = PIHOLE.get(hostname)
    if not cfg:
        return None
    base = cfg["base_url"].rstrip("/")
    password = cfg.get("password", "")
    with _pihole_lock:
        try:
            return _pihole_v6(base, password)
        except Exception:
            pass
        try:
            return _pihole_v5(base, password)
        except Exception:
            return None


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
    cpu_percent = psutil.cpu_percent(interval=1)
    temp = get_cpu_temp()

    record_sample(cpu_percent, temp, ram.percent, disk.percent)

    network = get_tailscale_status()
    network.update(get_network_throughput())

    payload = {
        "hostname": hostname,
        "role": ROLES.get(hostname, "Node"),
        "uptime": get_uptime(),
        "cpu": {
            "percent": cpu_percent,
            "temp_celsius": temp,
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
        "network": network,
        "containers": get_services(hostname),
        "timestamp": datetime.datetime.now().isoformat(),
    }

    pihole = get_pihole_stats(hostname)
    if pihole:
        payload["pihole"] = pihole

    return jsonify(payload)


@app.route("/history")
def history():
    try:
        hours = int(request.args.get("hours", 24))
    except (TypeError, ValueError):
        hours = 24
    hours = max(1, min(hours, HISTORY_RETENTION_DAYS * 24))
    return jsonify(get_history(hours))


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
