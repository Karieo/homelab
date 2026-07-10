"""Tests for the alerter's disk/service checks and state persistence.

The alerter's transport and notifier are monkeypatched, so these run with no
network and no notification channels. Run with:

    cd dashboard && pytest tests -q
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alerter  # noqa: E402


def run_poll(monkeypatch, tmp_path, payloads, prior_state=None):
    """Run alerter.check once per (host, payload); returns notify titles.

    A payload of None simulates an unreachable node (fetch raises).
    """
    sent = []
    monkeypatch.setattr(alerter, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(alerter, "notify",
                        lambda title, msg, **kw: sent.append(title))
    alerter.state.clear()
    alerter.state.update(prior_state or {})
    for host, payload in payloads:
        if payload is None:
            monkeypatch.setattr(
                alerter, "_fetch",
                lambda url: (_ for _ in ()).throw(OSError("down")))
        else:
            monkeypatch.setattr(alerter, "_fetch", lambda url, p=payload: p)
        alerter.check(host, "http://x")
    return sent


BASE = {"cpu": {"temp_celsius": 40}, "disk": {"percent": 50},
        "extra_disks": [], "containers": []}


def test_disk_full_fires_once_and_clears(monkeypatch, tmp_path):
    full = dict(BASE, disk={"percent": 95})
    sent = run_poll(monkeypatch, tmp_path,
                    [("scout", full), ("scout", full),
                     ("scout", dict(BASE, disk={"percent": 50}))])
    assert sum("FULL" in t for t in sent) == 1
    assert sum("ok" in t for t in sent) == 1


def test_extra_disk_tracked_separately(monkeypatch, tmp_path):
    payload = dict(BASE, extra_disks=[{"name": "T7", "percent": 96}])
    sent = run_poll(monkeypatch, tmp_path, [("bastion", payload)])
    assert any("disk T7 FULL" in t for t in sent)
    assert not any("disk FULL" in t for t in sent)  # root disk at 50% is fine


def test_service_down_debounced(monkeypatch, tmp_path):
    down = dict(BASE, containers=[{"name": "pihole", "status": "stopped"}])
    up = dict(BASE, containers=[{"name": "pihole", "status": "running"}])
    sent = run_poll(monkeypatch, tmp_path, [("scout", down)])
    assert not any("DOWN" in t for t in sent)  # single poll: debounced
    sent = run_poll(monkeypatch, tmp_path,
                    [("scout", down), ("scout", down), ("scout", up)])
    assert sum("DOWN" in t for t in sent) == 1
    assert sum("back UP" in t for t in sent) == 1


def test_removed_service_never_alerts(monkeypatch, tmp_path):
    down = dict(BASE, containers=[{"name": "old", "status": "stopped"}])
    gone = dict(BASE, containers=[])
    sent = run_poll(monkeypatch, tmp_path,
                    [("scout", down), ("scout", down), ("scout", gone)])
    assert sum("DOWN" in t for t in sent) == 1
    assert not any("back UP" in t for t in sent)


def test_no_temp_sensor_still_checks_disk(monkeypatch, tmp_path):
    # Nodes without a readable CPU temp must still get disk/service alerts.
    payload = {"cpu": {"temp_celsius": None}, "disk": {"percent": 95},
               "extra_disks": [], "containers": []}
    sent = run_poll(monkeypatch, tmp_path, [("scout", payload)])
    assert any("FULL" in t for t in sent)


def test_offline_does_not_advance_service_counters(monkeypatch, tmp_path):
    down = dict(BASE, containers=[{"name": "pihole", "status": "stopped"}])
    sent = run_poll(monkeypatch, tmp_path,
                    [("scout", down), ("scout", None), ("scout", None)])
    # One stopped poll + two unreachable polls != two stopped polls.
    assert not any("service pihole DOWN" in t for t in sent)


def test_state_persists_across_restart(monkeypatch, tmp_path):
    hot = dict(BASE, cpu={"temp_celsius": 95})
    sent = run_poll(monkeypatch, tmp_path, [("scout", hot)])
    assert sum("HOT" in t for t in sent) == 1
    # Simulate a restart: reload state from disk, poll again while still hot.
    saved = json.load(open(tmp_path / "state.json"))
    sent = run_poll(monkeypatch, tmp_path, [("scout", hot)],
                    prior_state=saved)
    assert not any("HOT" in t for t in sent)  # no duplicate re-fire


def test_corrupt_state_file_starts_fresh(monkeypatch, tmp_path):
    bad = tmp_path / "state.json"
    bad.write_text("{")
    monkeypatch.setattr(alerter, "STATE_FILE", str(bad))
    assert alerter._load_state() == {}
