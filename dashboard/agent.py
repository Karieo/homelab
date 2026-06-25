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
# Persisted list of AP client MACs to block (one per line). Re-applied at
# startup since iptables rules don't survive a reboot.
BLOCKLIST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "blocked-macs"
)

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
    "kiwix": 8090,
}

# Native (non-Docker) services to report per host, checked via
# `systemctl is-active`. Some nodes run things like Pi-hole or Kiwix as
# plain systemd services rather than containers; list them here so they
# show up alongside Docker containers on the dashboard. `port` builds the
# click-through link (set to None for no link).
EXTRA_SERVICES = {
    "scout": [
        {"name": "pihole", "unit": "pihole-FTL.service", "port": 80},
        {"name": "kiwix", "unit": "kiwix.service", "port": 8090},
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

# Extra mounted disks to report per host (beyond root). Each is shown as
# its own usage row. Unmounted/missing paths are skipped silently, so it's
# safe to list a drive before it's plugged in (e.g. the Samsung T7).
EXTRA_DISKS = {
    "bastion": [
        {"name": "T7", "path": "/mnt/t7"},
    ],
}

# Jellyfin "now playing", per host. Provide an API key via the
# JELLYFIN_API_KEY env var. Leave a host out to disable.
JELLYFIN = {
    "bastion": {
        "base_url": os.environ.get("JELLYFIN_BASE_URL", "http://localhost:8096"),
        "api_key": os.environ.get("JELLYFIN_API_KEY", ""),
    },
}

# Remndrs open-reminder count, per host. The self-hosted Remndrs instance
# exposes a count endpoint; point `count_url` at it and read `count_field`
# out of the JSON response. Token optional (sent as a Bearer header).
# Configure via env so no secret is committed.
REMNDRS = {
    "bastion": {
        "count_url": os.environ.get("REMNDRS_COUNT_URL", ""),
        "count_field": os.environ.get("REMNDRS_COUNT_FIELD", "count"),
        "token": os.environ.get("REMNDRS_TOKEN", ""),
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

# Wireless interfaces managed by the WiFi setup panel (NetworkManager).
# WIFI_IFACE is the upstream/client radio; WIFI_AP_IFACE is the radio used
# to re-broadcast (repeater/travel-router mode) — typically a USB adapter.
WIFI_IFACE = os.environ.get("WIFI_IFACE", "wlan0")
WIFI_AP_IFACE = os.environ.get("WIFI_AP_IFACE", "wlan1")
AP_CON_NAME = "dashboard-repeater-ap"
_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

# Friendly names for AP clients, keyed by lowercase MAC. DHCP hostnames are
# often missing — notably iOS "Private Wi-Fi Address", which also randomizes
# the MAC — so the connected-devices list falls back to the raw MAC. Fill in
# entries here to show names instead. iOS keeps a *stable* private MAC per
# SSID, so a name you set here sticks for that network.
#   "ca:57:86:80:5a:3e": "Clay's iPhone",
KNOWN_DEVICES = {
    "f2:15:67:f6:d0:ea": "Work phone",
    "a6:79:f7:2a:90:33": "Personal",
}


@app.after_request
def add_cors_headers(response):
    """Allow the dashboard (served from another origin) to fetch us."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
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
    """Return the node's Tailscale IP, MagicDNS hostname, and state.

    The MagicDNS hostname (e.g. "bastion.tailnet.ts.net") is preferred for
    building service links so they resolve from anywhere on the tailnet.
    """
    info = {
        "tailscale_ip": None,
        "tailscale_hostname": None,
        "tailscale_status": "disconnected",
    }
    # `tailscale status --json` gives both the IP and the MagicDNS name.
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self_node = json.loads(result.stdout).get("Self", {})
        ips = self_node.get("TailscaleIPs") or []
        dns = (self_node.get("DNSName") or "").rstrip(".")
        if ips:
            info["tailscale_ip"] = ips[0]
        if dns:
            info["tailscale_hostname"] = dns
        if ips or dns:
            info["tailscale_status"] = "connected"
            return info
    except Exception:
        pass
    # Fallback: at least get the IP from `tailscale ip -4`.
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        ip = result.stdout.strip().split("\n")[0]
        if ip:
            info["tailscale_ip"] = ip
            info["tailscale_status"] = "connected"
    except Exception:
        pass
    return info


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


# ---- Extra disks -------------------------------------------------------

def get_extra_disks(hostname):
    """Report usage for configured extra mounts that are actually present."""
    disks = []
    for d in EXTRA_DISKS.get(hostname, []):
        path = d["path"]
        if not os.path.ismount(path) and not os.path.isdir(path):
            continue
        try:
            usage = psutil.disk_usage(path)
        except Exception:
            continue
        disks.append(
            {
                "name": d["name"],
                "used_gb": round(usage.used / 1e9, 1),
                "total_gb": round(usage.total / 1e9, 1),
                "percent": usage.percent,
            }
        )
    return disks


# ---- Jellyfin now-playing ---------------------------------------------

def get_jellyfin_now_playing(hostname):
    """Return a list of currently-playing Jellyfin sessions, or None."""
    cfg = JELLYFIN.get(hostname)
    if not cfg or not cfg.get("api_key"):
        return None
    base = cfg["base_url"].rstrip("/")
    try:
        sessions = _http_json(
            base + "/Sessions",
            headers={"X-Emby-Token": cfg["api_key"]},
        )
    except Exception:
        return None

    playing = []
    for s in sessions or []:
        item = s.get("NowPlayingItem")
        if not item:
            continue
        # Prefer "Series — Episode" when it's a TV episode.
        title = item.get("Name", "?")
        series = item.get("SeriesName")
        if series:
            title = f"{series} — {title}"
        runtime = item.get("RunTimeTicks") or 0
        position = (s.get("PlayState") or {}).get("PositionTicks") or 0
        percent = round(position / runtime * 100, 1) if runtime else None
        playing.append(
            {
                "title": title,
                "user": s.get("UserName"),
                "percent": percent,
                "paused": (s.get("PlayState") or {}).get("IsPaused", False),
            }
        )
    return playing


# ---- Remndrs open-reminder count --------------------------------------

def get_remndrs_count(hostname):
    """Return the count of open reminders, or None if not configured/up."""
    cfg = REMNDRS.get(hostname)
    if not cfg or not cfg.get("count_url"):
        return None
    headers = {}
    if cfg.get("token"):
        headers["Authorization"] = "Bearer " + cfg["token"]
    try:
        res = _http_json(cfg["count_url"], headers=headers)
    except Exception:
        return None
    # Accept either a bare number or an object with the configured field.
    if isinstance(res, (int, float)):
        return int(res)
    if isinstance(res, dict):
        val = res.get(cfg.get("count_field", "count"))
        if isinstance(val, list):
            return len(val)
        if isinstance(val, (int, float)):
            return int(val)
    if isinstance(res, list):
        return len(res)
    return None


# ---- WiFi (NetworkManager) --------------------------------------------

def has_iface(iface):
    return os.path.exists(f"/sys/class/net/{iface}")


def default_route_ifaces():
    """Set of interfaces that currently carry a default route."""
    ifaces = set()
    try:
        result = subprocess.run(
            ["ip", "-o", "route", "show", "default"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if "dev" in parts:
                ifaces.add(parts[parts.index("dev") + 1])
    except Exception:
        pass
    return ifaces


def is_sole_uplink(iface):
    """True if `iface` is scout's *only* path to the network (default route).

    Reconfiguring such an interface would cut off remote management, so the
    WiFi endpoints refuse to touch it unless explicitly forced.
    """
    routes = default_route_ifaces()
    return routes == {iface}


def get_wifi_status(iface=None):
    """Return the wireless interface's current connection state."""
    iface = iface or WIFI_IFACE
    info = {
        "iface": iface,
        "connected": False,
        "ssid": None,
        "ip": None,
        "signal": None,
    }
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f",
             "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS", "device", "show",
             iface],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if line.startswith("GENERAL.CONNECTION:"):
                con = line.split(":", 1)[1].strip()
                if con and con != "--":
                    info["ssid"] = con
                    info["connected"] = True
            elif line.startswith("IP4.ADDRESS"):
                info["ip"] = line.split(":", 1)[1].split("/")[0].strip() or None
    except Exception:
        return info
    # Signal strength of the active AP.
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SIGNAL", "device", "wifi"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if parts and parts[0] == "yes" and len(parts) > 1:
                info["signal"] = parts[1]
                break
    except Exception:
        pass
    # Repeater AP status, when a second radio is present.
    if has_iface(WIFI_AP_IFACE):
        info["ap"] = get_ap_status()
    return info


def _ap_leases():
    """Map MAC -> {ip, hostname} from the AP's NetworkManager dnsmasq leases.

    NetworkManager's shared (AP) mode runs a dnsmasq whose leases live at a
    predictable path. Best-effort: if the file is missing or unreadable we
    just return what we have (stations still get MAC + signal from `iw`).
    """
    path = "/var/lib/NetworkManager/dnsmasq-%s.leases" % AP_CON_NAME
    leases = {}
    try:
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4:
                    host = parts[3]
                    leases[parts[1].lower()] = {
                        "ip": parts[2],
                        "hostname": None if host == "*" else host,
                    }
    except Exception:
        pass
    return leases


def _ap_neigh(iface):
    """Map MAC -> IP from the kernel neighbour (ARP) table for an interface.

    Readable without root, unlike the dnsmasq lease file — so this is the
    reliable way to attach an IP to each connected station.
    """
    out = {}
    try:
        r = subprocess.run(
            ["ip", "-o", "neigh", "show", "dev", iface],
            capture_output=True, text=True, timeout=10,
        )
        for line in r.stdout.splitlines():
            # "10.42.0.203 lladdr ca:57:86:80:5a:3e REACHABLE"
            parts = line.split()
            if len(parts) >= 4 and parts[1] == "lladdr":
                out[parts[2].lower()] = parts[0]
    except Exception:
        pass
    return out


def get_ap_status(iface=None):
    """Return the broadcast (AP) radio's state and connected clients."""
    iface = iface or WIFI_AP_IFACE
    info = {"iface": iface, "active": False, "ssid": None, "clients": None}
    if not has_iface(iface):
        return info
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "device", "show", iface],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if line.startswith("GENERAL.CONNECTION:"):
                con = line.split(":", 1)[1].strip()
                if con and con != "--":
                    info["active"] = True
                    # Resolve the broadcast SSID from the profile.
                    s = subprocess.run(
                        ["nmcli", "-g", "802-11-wireless.ssid", "connection",
                         "show", con],
                        capture_output=True, text=True, timeout=10,
                    )
                    info["ssid"] = s.stdout.strip() or con
    except Exception:
        return info
    # Associated clients via iw (best-effort; needs root). Enrich each with
    # IP/hostname from the AP's DHCP leases so the dashboard can show which
    # devices are connected, not just a count.
    try:
        result = subprocess.run(
            ["sudo", "-n", "iw", "dev", iface, "station", "dump"],
            capture_output=True, text=True, timeout=10,
        )
        leases = _ap_leases()
        neigh = _ap_neigh(iface)
        stations = []
        cur = None
        for ln in result.stdout.splitlines():
            if ln.startswith("Station"):
                mac = ln.split()[1].lower()
                lease = leases.get(mac, {})
                cur = {
                    "mac": mac,
                    # ARP table is root-free; lease file (if readable) as backup.
                    "ip": neigh.get(mac) or lease.get("ip"),
                    # Curated name wins over a DHCP-provided hostname.
                    "hostname": KNOWN_DEVICES.get(mac) or lease.get("hostname"),
                    "signal_dbm": None,
                    "connected_seconds": None,
                    "connected": True,
                }
                stations.append(cur)
            elif cur is not None:
                s = ln.strip()
                if s.startswith("signal:"):
                    try:
                        cur["signal_dbm"] = int(s.split(":", 1)[1].split()[0])
                    except Exception:
                        pass
                elif s.startswith("connected time:"):
                    try:
                        cur["connected_seconds"] = int(s.split(":", 1)[1].split()[0])
                    except Exception:
                        pass
        info["clients"] = len(stations)
        # Flag blocked clients, and surface any blocked MAC that isn't currently
        # associated (so it can still be unblocked from the dashboard).
        blocked = set(_read_blocklist())
        present = {st["mac"] for st in stations}
        for st in stations:
            st["blocked"] = st["mac"] in blocked
        for mac in blocked - present:
            stations.append({
                "mac": mac, "ip": None,
                "hostname": KNOWN_DEVICES.get(mac),
                "signal_dbm": None, "connected_seconds": None,
                "connected": False, "blocked": True,
            })
        info["stations"] = stations
    except Exception:
        pass
    return info


# ---- AP client block/unblock (travel-router "timeout a device") --------

def _read_blocklist():
    try:
        with open(BLOCKLIST_PATH) as f:
            return [ln.strip().lower() for ln in f if _MAC_RE.match(ln.strip())]
    except Exception:
        return []


def _write_blocklist(macs):
    try:
        uniq = sorted(set(macs))
        with open(BLOCKLIST_PATH, "w") as f:
            f.write("\n".join(uniq) + ("\n" if uniq else ""))
    except Exception:
        pass


def _block_rule_present(mac):
    r = subprocess.run(
        ["sudo", "-n", "iptables", "-C", "FORWARD", "-m", "mac",
         "--mac-source", mac, "-j", "DROP"],
        capture_output=True, text=True, timeout=10,
    )
    return r.returncode == 0


def _set_block_rule(mac, add):
    """Add/remove a FORWARD DROP for a client MAC (idempotent)."""
    if add and _block_rule_present(mac):
        return True
    if not add and not _block_rule_present(mac):
        return True
    r = subprocess.run(
        ["sudo", "-n", "iptables", "-I" if add else "-D", "FORWARD",
         "-m", "mac", "--mac-source", mac, "-j", "DROP"],
        capture_output=True, text=True, timeout=10,
    )
    return r.returncode == 0


def wifi_block_client(mac):
    mac = (mac or "").strip().lower()
    if not _MAC_RE.match(mac):
        return {"ok": False, "error": "invalid MAC address"}
    if not _set_block_rule(mac, add=True):
        return {"ok": False,
                "error": "couldn't add firewall rule (is iptables in the "
                         "agent's sudoers?)"}
    # Kick it off now; the DROP keeps it off the internet if it rejoins.
    subprocess.run(
        ["sudo", "-n", "iw", "dev", WIFI_AP_IFACE, "station", "del", mac],
        capture_output=True, text=True, timeout=10,
    )
    macs = _read_blocklist()
    if mac not in macs:
        macs.append(mac)
        _write_blocklist(macs)
    return {"ok": True, "message": f"Blocked {mac}"}


def wifi_unblock_client(mac):
    mac = (mac or "").strip().lower()
    if not _MAC_RE.match(mac):
        return {"ok": False, "error": "invalid MAC address"}
    _set_block_rule(mac, add=False)
    _write_blocklist([m for m in _read_blocklist() if m != mac])
    return {"ok": True, "message": f"Unblocked {mac}"}


def reapply_blocklist():
    """Re-add DROP rules for persisted blocks (iptables is cleared on reboot)."""
    for mac in _read_blocklist():
        _set_block_rule(mac, add=True)


def wifi_connect(ssid, password, clone_mac, mac, iface=None, username=""):
    """Create/refresh an nmcli wifi client profile and bring it up.

    Supports WPA-PSK (password) and WPA-Enterprise (username + password,
    PEAP/MSCHAPv2). Runs nmcli via sudo; all values are passed as argv
    (never a shell string), so SSID/credentials can't inject commands.
    """
    iface = iface or WIFI_IFACE
    con = ssid

    # Replace any stale profile of the same name so settings are clean.
    subprocess.run(
        ["sudo", "-n", "nmcli", "connection", "delete", con],
        capture_output=True, text=True, timeout=20,
    )

    add_cmd = [
        "sudo", "-n", "nmcli", "connection", "add", "type", "wifi",
        "ifname", iface, "con-name", con, "ssid", ssid,
    ]
    if username:
        # WPA-Enterprise (e.g. campus/corporate) — PEAP + MSCHAPv2.
        add_cmd += [
            "wifi-sec.key-mgmt", "wpa-eap",
            "802-1x.eap", "peap", "802-1x.phase2-auth", "mschapv2",
            "802-1x.identity", username,
        ]
        if password:
            add_cmd += ["802-1x.password", password]
    elif password:
        add_cmd += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    if clone_mac and mac:
        add_cmd += ["802-11-wireless.cloned-mac-address", mac]

    r = subprocess.run(add_cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or r.stdout).strip()}

    r2 = subprocess.run(
        ["sudo", "-n", "nmcli", "connection", "up", con],
        capture_output=True, text=True, timeout=90,
    )
    if r2.returncode != 0:
        # Leave no half-activated profile behind on failure.
        subprocess.run(
            ["sudo", "-n", "nmcli", "connection", "delete", con],
            capture_output=True, text=True, timeout=20,
        )
        return {"ok": False, "error": (r2.stderr or r2.stdout).strip()}

    return {"ok": True, "message": (r2.stdout or "Connected").strip()}


def wifi_start_repeater(up_ssid, up_password, up_username, clone_mac, mac,
                        ap_ssid, ap_password, keep_upstream=False):
    """Re-broadcast WIFI_IFACE's connection as a NAT'd access point on
    WIFI_AP_IFACE (travel-router / repeater).

    With keep_upstream=True the existing WIFI_IFACE connection is left
    untouched (it just needs to be online) and only the AP is brought up —
    this is the safe default when WIFI_IFACE is already on the network you
    want to repeat (and avoids ever cutting scout's own uplink). Otherwise
    WIFI_IFACE is (re)connected to the given upstream first.
    """
    if keep_upstream:
        status = get_wifi_status(WIFI_IFACE)
        if not status.get("connected"):
            return {"ok": False, "stage": "upstream",
                    "error": f"{WIFI_IFACE} isn't connected to anything to "
                             f"repeat — connect it first or uncheck "
                             f"'keep current connection'."}
        up_label = status.get("ssid") or WIFI_IFACE
    else:
        up = wifi_connect(up_ssid, up_password, clone_mac, mac,
                          iface=WIFI_IFACE, username=up_username)
        if not up.get("ok"):
            return {"ok": False, "stage": "upstream", "error": up.get("error")}
        up_label = up_ssid

    if not has_iface(WIFI_AP_IFACE):
        return {"ok": False, "stage": "ap",
                "error": f"no AP interface {WIFI_AP_IFACE}"}

    subprocess.run(
        ["sudo", "-n", "nmcli", "connection", "delete", AP_CON_NAME],
        capture_output=True, text=True, timeout=20,
    )
    add_cmd = [
        "sudo", "-n", "nmcli", "connection", "add", "type", "wifi",
        "ifname", WIFI_AP_IFACE, "con-name", AP_CON_NAME, "ssid", ap_ssid,
        "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
        "ipv4.method", "shared",
        # Persist across reboots so scout always self-hosts this AP at boot,
        # independent of any upstream — that's the management network you join
        # to reach the dashboard and pick a new upstream from a fresh location.
        "connection.autoconnect", "yes",
        "connection.autoconnect-priority", "100",
    ]
    if ap_password:
        # Pin a clean WPA2-PSK config (RSN + CCMP, PMF off). This is the
        # most broadly compatible AP setup; NetworkManager's defaults can
        # negotiate mixed proto/cipher or 802.11w (PMF), which many clients
        # (notably iOS) reject mid-handshake and report as a wrong password.
        add_cmd += [
            "wifi-sec.key-mgmt", "wpa-psk",
            "wifi-sec.proto", "rsn",
            "wifi-sec.pairwise", "ccmp",
            "wifi-sec.group", "ccmp",
            "wifi-sec.pmf", "1",
            "wifi-sec.psk", ap_password,
        ]

    r = subprocess.run(add_cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return {"ok": False, "stage": "ap", "error": (r.stderr or r.stdout).strip()}

    r2 = subprocess.run(
        ["sudo", "-n", "nmcli", "connection", "up", AP_CON_NAME],
        capture_output=True, text=True, timeout=60,
    )
    if r2.returncode != 0:
        subprocess.run(
            ["sudo", "-n", "nmcli", "connection", "delete", AP_CON_NAME],
            capture_output=True, text=True, timeout=20,
        )
        return {"ok": False, "stage": "ap", "error": (r2.stderr or r2.stdout).strip()}

    return {"ok": True, "message": f"Repeating {up_label} → {ap_ssid}"}


def wifi_stop_ap():
    """Bring the repeater AP down (upstream client stays connected)."""
    subprocess.run(
        ["sudo", "-n", "nmcli", "connection", "down", AP_CON_NAME],
        capture_output=True, text=True, timeout=30,
    )
    return {"ok": True, "message": "Repeater AP stopped"}


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
        "extra_disks": get_extra_disks(hostname),
        "timestamp": datetime.datetime.now().isoformat(),
    }

    pihole = get_pihole_stats(hostname)
    if pihole:
        payload["pihole"] = pihole

    jellyfin = get_jellyfin_now_playing(hostname)
    if jellyfin is not None:
        payload["jellyfin"] = jellyfin

    remndrs = get_remndrs_count(hostname)
    if remndrs is not None:
        payload["remndrs_open"] = remndrs

    if has_iface(WIFI_IFACE):
        payload["wifi"] = get_wifi_status()

    return jsonify(payload)


@app.route("/wifi/status")
def wifi_status():
    if not has_iface(WIFI_IFACE):
        return jsonify({"error": "no wifi interface"}), 400
    return jsonify(get_wifi_status())


@app.route("/wifi/connect", methods=["POST", "OPTIONS"])
def wifi_connect_route():
    if request.method == "OPTIONS":
        return ("", 204)
    if not has_iface(WIFI_IFACE):
        return jsonify({"ok": False, "error": "no wifi interface"}), 400

    body = request.get_json(silent=True) or {}
    ssid = (body.get("ssid") or "").strip()
    password = body.get("password") or ""
    username = (body.get("username") or "").strip()
    clone_mac = bool(body.get("clone_mac"))
    mac = (body.get("mac") or "").strip()
    force = bool(body.get("force"))

    if not ssid:
        return jsonify({"ok": False, "error": "SSID is required"}), 400
    if clone_mac and not _MAC_RE.match(mac):
        return jsonify({"ok": False, "error": "Invalid MAC address"}), 400
    if is_sole_uplink(WIFI_IFACE) and not force:
        return jsonify({
            "ok": False, "needs_force": True,
            "error": f"{WIFI_IFACE} is this node's only network connection — "
                     f"reconfiguring it would cut off access. Connect Ethernet "
                     f"first, or resend with force=true.",
        }), 409

    result = wifi_connect(ssid, password, clone_mac, mac, username=username)
    result["status"] = get_wifi_status()
    return jsonify(result), (200 if result.get("ok") else 502)


@app.route("/wifi/repeater", methods=["POST", "OPTIONS"])
def wifi_repeater_route():
    if request.method == "OPTIONS":
        return ("", 204)
    if not has_iface(WIFI_IFACE):
        return jsonify({"ok": False, "error": "no wifi interface"}), 400

    body = request.get_json(silent=True) or {}
    up_ssid = (body.get("up_ssid") or "").strip()
    up_password = body.get("up_password") or ""
    up_username = (body.get("up_username") or "").strip()
    clone_mac = bool(body.get("clone_mac"))
    mac = (body.get("mac") or "").strip()
    ap_ssid = (body.get("ap_ssid") or "").strip()
    ap_password = body.get("ap_password") or ""
    keep_upstream = bool(body.get("keep_upstream"))
    force = bool(body.get("force"))

    if not ap_ssid:
        return jsonify({"ok": False, "error": "Broadcast SSID is required"}), 400
    if ap_password and not (8 <= len(ap_password) <= 63):
        return jsonify({"ok": False,
                        "error": "Broadcast password must be 8-63 characters"}), 400
    # Only validate/guard the upstream when we're actually reconfiguring it.
    if not keep_upstream:
        if not up_ssid:
            return jsonify({"ok": False, "error": "Upstream SSID is required"}), 400
        if clone_mac and not _MAC_RE.match(mac):
            return jsonify({"ok": False, "error": "Invalid MAC address"}), 400
        if is_sole_uplink(WIFI_IFACE) and not force:
            return jsonify({
                "ok": False, "needs_force": True,
                "error": f"{WIFI_IFACE} is this node's only network connection — "
                         f"reconfiguring the upstream would cut off access. Tick "
                         f"'keep current connection', connect Ethernet, or resend "
                         f"with force=true.",
            }), 409

    result = wifi_start_repeater(up_ssid, up_password, up_username, clone_mac,
                                 mac, ap_ssid, ap_password,
                                 keep_upstream=keep_upstream)
    result["status"] = get_wifi_status()
    return jsonify(result), (200 if result.get("ok") else 502)


@app.route("/wifi/stop", methods=["POST", "OPTIONS"])
def wifi_stop_route():
    if request.method == "OPTIONS":
        return ("", 204)
    result = wifi_stop_ap()
    result["status"] = get_wifi_status()
    return jsonify(result)


@app.route("/wifi/block", methods=["POST", "OPTIONS"])
def wifi_block_route():
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(silent=True) or {}
    return jsonify(wifi_block_client(body.get("mac")))


@app.route("/wifi/unblock", methods=["POST", "OPTIONS"])
def wifi_unblock_route():
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(silent=True) or {}
    return jsonify(wifi_unblock_client(body.get("mac")))


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
reapply_blocklist()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
