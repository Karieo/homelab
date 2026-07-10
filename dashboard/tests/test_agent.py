"""Smoke tests for agent.py endpoints, plus the Meshtastic collector.

Everything here is safe to run on a bare CI runner: no Docker, tailscale,
nmcli, wlan interfaces, or a Meshtastic radio required — the agent degrades
gracefully around all of them, and these tests pin that behavior. Run with:

    cd dashboard && pytest tests -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402


def client():
    agent.app.config["TESTING"] = True
    return agent.app.test_client()


# ---- Endpoint smoke tests ----------------------------------------------

def test_health():
    res = client().get("/health")
    assert res.status_code == 200
    assert res.get_json() == {"status": "ok"}


def test_stats_shape():
    res = client().get("/stats")
    assert res.status_code == 200
    data = res.get_json()
    for key in ("hostname", "role", "uptime", "cpu", "ram", "disk",
                "network", "containers", "timestamp"):
        assert key in data, f"missing {key}"
    assert isinstance(data["cpu"]["percent"], (int, float))
    assert isinstance(data["containers"], list)


def test_history_shape():
    res = client().get("/history?hours=24")
    assert res.status_code == 200
    data = res.get_json()
    for key in ("ts", "cpu", "temp", "ram", "disk"):
        assert isinstance(data[key], list)


def test_history_bad_hours_falls_back():
    res = client().get("/history?hours=banana")
    assert res.status_code == 200


def test_cors_headers():
    res = client().get("/health")
    assert res.headers["Access-Control-Allow-Origin"] == "*"


def test_wifi_routes_without_interface():
    # CI runners have no wlan0, so these must 400 cleanly (and must NOT
    # reach any sudo/nmcli code path).
    c = client()
    assert c.get("/wifi/status").status_code == 400
    assert c.post("/wifi/connect", json={"ssid": "x"}).status_code == 400


# ---- Trusted-source gate on mutating WiFi endpoints ---------------------

def test_wifi_mutations_blocked_from_untrusted_ip():
    c = client()
    for path in ("/wifi/connect", "/wifi/repeater", "/wifi/stop",
                 "/wifi/block", "/wifi/unblock", "/wifi/block-unknown"):
        res = c.post(path, json={}, environ_base={"REMOTE_ADDR": "203.0.113.9"})
        assert res.status_code == 403, path
        assert res.get_json()["ok"] is False


def test_wifi_mutations_pass_gate_from_localhost():
    c = client()
    # Localhost must get PAST the gate. With no wlan0 on the CI runner the
    # route then fails its own interface check (400), never 403.
    res = c.post("/wifi/connect", json={"ssid": "x"},
                 environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert res.status_code == 400


def test_wifi_gate_allows_tailscale_and_ap_subnet():
    c = client()
    for addr in ("100.101.102.103", "10.42.0.55"):
        res = c.post("/wifi/connect", json={"ssid": "x"},
                     environ_base={"REMOTE_ADDR": addr})
        assert res.status_code == 400, addr  # past the gate, no iface → 400


def test_wifi_gate_strips_ipv4_mapped_ipv6():
    c = client()
    res = c.post("/wifi/connect", json={"ssid": "x"},
                 environ_base={"REMOTE_ADDR": "::ffff:127.0.0.1"})
    assert res.status_code == 400  # trusted after stripping the prefix


def test_wifi_gate_preflight_passes():
    c = client()
    res = c.open("/wifi/connect", method="OPTIONS",
                 environ_base={"REMOTE_ADDR": "203.0.113.9"})
    assert res.status_code == 204


# ---- Meshtastic collector ----------------------------------------------

def test_meshtastic_unconfigured_host_returns_none():
    assert agent.get_meshtastic_status("not-a-configured-host") is None


def test_meshtastic_unavailable_is_silently_none(monkeypatch):
    # Configured host but no radio/package on the test runner: must return
    # None quickly and never raise.
    agent.MESHTASTIC["testhost"] = {"host": "127.0.0.1"}
    try:
        agent._mesh_cache["ts"] = 0
        assert agent.get_meshtastic_status("testhost") is None
    finally:
        del agent.MESHTASTIC["testhost"]


def test_meshtastic_result_is_cached(monkeypatch):
    calls = []

    def fake_fetch(host):
        calls.append(host)
        return {"nodes": 3, "battery": 101, "voltage": 4.1,
                "channel_util": 1.2, "air_util_tx": 0.4, "last_heard": 100}

    monkeypatch.setattr(agent, "_mesh_fetch", fake_fetch)
    agent.MESHTASTIC["testhost2"] = {"host": "127.0.0.1"}
    try:
        agent._mesh_cache["ts"] = 0
        first = agent.get_meshtastic_status("testhost2")
        second = agent.get_meshtastic_status("testhost2")
        assert first == second == {
            "nodes": 3, "battery": 101, "voltage": 4.1,
            "channel_util": 1.2, "air_util_tx": 0.4, "last_heard": 100,
        }
        assert len(calls) == 1  # second call hit the 60s cache, not _mesh_fetch
    finally:
        del agent.MESHTASTIC["testhost2"]


def test_extra_services_skips_optional_when_unit_missing(monkeypatch):
    monkeypatch.setattr(agent, "_unit_exists", lambda unit: False)
    monkeypatch.setattr(agent, "_systemd_active", lambda unit: False)
    services = agent.get_extra_services("scout")
    names = [s["name"] for s in services]
    assert "meshtastic" not in names
    assert "pihole" in names  # non-optional entries unaffected


def test_extra_services_includes_optional_when_unit_present(monkeypatch):
    monkeypatch.setattr(agent, "_unit_exists", lambda unit: True)
    monkeypatch.setattr(agent, "_systemd_active", lambda unit: False)
    services = agent.get_extra_services("bastion")
    names = [s["name"] for s in services]
    assert "meshtastic" in names
    mesh = next(s for s in services if s["name"] == "meshtastic")
    assert mesh["status"] == "stopped"  # unit exists but inactive
