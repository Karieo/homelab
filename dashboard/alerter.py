#!/usr/bin/env python3
"""Homelab alerter.

Polls each node's stats agent and pushes a notification (via ntfy) when:
  - a node goes offline (unreachable) or comes back online, or
  - CPU temperature crosses a high threshold (with hysteresis so a
    persistently-hot node doesn't spam).

Runs as a systemd service on bastion. State is in-memory; alerts fire on
transitions only. With no ntfy channel configured it logs to stdout, so
it's safe to run before you've set NTFY_URL.

Notifications go to every configured channel (and always to stdout). With
none configured it just logs.

Config via environment (set in the systemd unit):
  DISCORD_WEBHOOK_URL Discord channel webhook URL (Server Settings →
                      Integrations → Webhooks)
  NTFY_URL            e.g. https://ntfy.sh/my-homelab-topic
  NTFY_TOKEN          optional bearer token for a protected ntfy server
  ALERT_NODES         "bastion=http://localhost:9090/stats,scout=http://scout:9090/stats"
  ALERT_POLL_SEC      poll interval, default 60
  ALERT_TEMP_HIGH     fire at/above this °C, default 80
  ALERT_TEMP_CLEAR    clear at/below this °C, default 72
  ALERT_OFFLINE_AFTER consecutive failures before "offline", default 2
"""

import json
import os
import time
import urllib.request


def _parse_nodes():
    raw = os.environ.get("ALERT_NODES", "").strip()
    if not raw:
        return {
            "bastion": "http://localhost:9090/stats",
            "scout": "http://scout:9090/stats",
        }
    nodes = {}
    for pair in raw.split(","):
        if "=" in pair:
            host, url = pair.split("=", 1)
            nodes[host.strip()] = url.strip()
    return nodes


NODES = _parse_nodes()
POLL_INTERVAL_SEC = int(os.environ.get("ALERT_POLL_SEC", "60"))
TEMP_HIGH_C = float(os.environ.get("ALERT_TEMP_HIGH", "80"))
TEMP_CLEAR_C = float(os.environ.get("ALERT_TEMP_CLEAR", "72"))
OFFLINE_AFTER = int(os.environ.get("ALERT_OFFLINE_AFTER", "2"))

NTFY_URL = os.environ.get("NTFY_URL", "").strip()
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "").strip()
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# Embed colors for Discord, by severity.
_DISCORD_COLORS = {"high": 0xE74C3C, "default": 0x2ECC71}

# host -> {"online": bool, "temp_high": bool, "fail": int}
state = {}


# Discord (Cloudflare) 403s the default "Python-urllib" User-Agent, so set one.
_UA = "homelab-ops-alerter/1.0"


def _send_ntfy(title, message, priority, tags):
    headers = {"Title": title, "Priority": priority, "User-Agent": _UA}
    if tags:
        headers["Tags"] = tags
    if NTFY_TOKEN:
        headers["Authorization"] = "Bearer " + NTFY_TOKEN
    req = urllib.request.Request(NTFY_URL, data=message.encode(), headers=headers)
    urllib.request.urlopen(req, timeout=10)


def _send_discord(title, message, priority):
    payload = {
        "embeds": [
            {
                "title": title,
                "description": message,
                "color": _DISCORD_COLORS.get(priority, _DISCORD_COLORS["default"]),
            }
        ]
    }
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": _UA},
    )
    urllib.request.urlopen(req, timeout=10)


def notify(title, message, priority="default", tags=""):
    """Send to every configured channel; always log to stdout."""
    print(f"[alert] {title}: {message}", flush=True)
    channels = []
    if NTFY_URL:
        channels.append(("ntfy", lambda: _send_ntfy(title, message, priority, tags)))
    if DISCORD_WEBHOOK_URL:
        channels.append(("discord", lambda: _send_discord(title, message, priority)))
    for name, send in channels:
        try:
            send()
        except Exception as e:
            print(f"notify via {name} failed:", e, flush=True)


def _fetch(url):
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode())


def check(host, url):
    st = state.setdefault(host, {"online": True, "temp_high": False, "fail": 0})

    try:
        data = _fetch(url)
    except Exception:
        st["fail"] += 1
        if st["online"] and st["fail"] >= OFFLINE_AFTER:
            st["online"] = False
            notify(
                f"\U0001f534 {host} OFFLINE",
                f"{host} is unreachable.",
                priority="high",
                tags="rotating_light",
            )
        return

    if not st["online"]:
        notify(
            f"\U0001f7e2 {host} back ONLINE",
            f"{host} is responding again.",
            tags="white_check_mark",
        )
    st["online"] = True
    st["fail"] = 0

    temp = (data.get("cpu") or {}).get("temp_celsius")
    if temp is None:
        return
    if not st["temp_high"] and temp >= TEMP_HIGH_C:
        st["temp_high"] = True
        notify(
            f"\U0001f321️ {host} CPU HOT",
            f"CPU temp {temp}°C (≥ {TEMP_HIGH_C}°C)",
            priority="high",
            tags="fire",
        )
    elif st["temp_high"] and temp <= TEMP_CLEAR_C:
        st["temp_high"] = False
        notify(
            f"❄️ {host} cooled down",
            f"CPU temp back to {temp}°C",
            tags="snowflake",
        )


def main():
    channels = []
    if DISCORD_WEBHOOK_URL:
        channels.append("discord")
    if NTFY_URL:
        channels.append("ntfy")
    print(
        f"alerter watching {list(NODES)} every {POLL_INTERVAL_SEC}s; "
        f"channels={','.join(channels) if channels else 'none (log only)'}; "
        f"temp high={TEMP_HIGH_C}°C clear={TEMP_CLEAR_C}°C",
        flush=True,
    )
    while True:
        for host, url in NODES.items():
            try:
                check(host, url)
            except Exception as e:
                print("check error", host, e, flush=True)
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
