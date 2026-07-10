"""Tests for the Meshtastic collector in agent.py.

Only the parts that are safe to exercise without a real radio or a
`meshtastic` package install: the unconfigured-host short-circuit and the
"failure is cached as None, never raises" behavior. Run with:

    cd dashboard && pytest tests -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402


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
