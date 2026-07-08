# PLAN: Alerter coverage — disk-full, service-down, persistent state

**Rank: 4 of 5 — independent of the other plans; needs only
PLAN-ci-safe-deploys (for the test harness) to be done first.**

## Goal

`dashboard/alerter.py` only alerts on two conditions: node offline and CPU
temperature. The two most common ways a homelab actually degrades are missed
entirely:

- **Disk filling up** (media, logs, docker images) — by the time you notice,
  services are already corrupting state.
- **A service dying** — `docker ps` shows it stopped, the dashboard shows a
  red dot, but nobody is looking at the dashboard. The agent already reports
  every service's status in `/stats`; the alerter just ignores it.

Also: alert state is **in-memory only**, and `update.sh` restarts
`dashboard-alerter` on **every auto-deploy** (~every merge to main). Every
deploy therefore wipes hysteresis state — a hot node re-fires its HIGH alert
after each deploy, and an offline node re-fires OFFLINE. Persist state to a
JSON file.

## Files to touch

| File | Action |
|------|--------|
| `dashboard/alerter.py` | **modify** (disk + service checks, state persistence) |
| `dashboard/tests/test_alerter.py` | **create** |
| `dashboard/systemd/dashboard-alerter.service` | **modify** (document new tunables) |
| `README.md` | **modify** (Alerts section: new conditions + tunables) |
| `.gitignore` | **modify** (add `dashboard/alerter-state.json`) |

## Step-by-step

### Step 1 — new tunables

Next to the existing threshold constants:

```python
DISK_HIGH_PCT = float(os.environ.get("ALERT_DISK_HIGH", "90"))
DISK_CLEAR_PCT = float(os.environ.get("ALERT_DISK_CLEAR", "85"))
SVC_DOWN_AFTER = int(os.environ.get("ALERT_SVC_AFTER", "2"))
STATE_FILE = os.environ.get(
    "ALERT_STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "alerter-state.json"),
)
```

### Step 2 — state persistence

Replace the bare `state = {}` with load/save helpers:

```python
def _load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print("state save failed:", e, flush=True)


state = _load_state()
```

Call `_save_state()` at the **end of every `check()` call** (state changes on
most polls anyway via fail counters; one small write per node per minute is
nothing).

### Step 3 — extend `check()`

The per-host state dict gains keys (use `st.setdefault(...)` so old persisted
files upgrade in place):

```python
st.setdefault("disk_high", {})   # disk name -> bool
st.setdefault("svc_down", {})    # service name -> bool (currently alerted)
st.setdefault("svc_fail", {})    # service name -> consecutive stopped polls
```

After the existing temperature block, add:

**Disk checks** — root plus extra disks, same hysteresis pattern as temp:

```python
    disks = [("disk", (data.get("disk") or {}).get("percent"))]
    for d in data.get("extra_disks") or []:
        disks.append((f"disk {d.get('name')}", d.get("percent")))
    for name, pct in disks:
        if pct is None:
            continue
        was_high = st["disk_high"].get(name, False)
        if not was_high and pct >= DISK_HIGH_PCT:
            st["disk_high"][name] = True
            notify(f"\U0001f4be {host} {name} FULL",
                   f"{name} at {pct}% (≥ {DISK_HIGH_PCT}%)",
                   priority="high", tags="floppy_disk")
        elif was_high and pct <= DISK_CLEAR_PCT:
            st["disk_high"][name] = False
            notify(f"\U0001f4be {host} {name} ok",
                   f"{name} back to {pct}%", tags="white_check_mark")
```

**Service checks** — transition-based with a 2-poll debounce (containers
restart during deploys; one stopped poll is normal):

```python
    seen = set()
    for svc in data.get("containers") or []:
        name = svc.get("name")
        if not name:
            continue
        seen.add(name)
        if svc.get("status") == "running":
            if st["svc_down"].get(name):
                notify(f"\U0001f7e2 {host} service {name} back UP",
                       f"{name} is running again.", tags="white_check_mark")
            st["svc_down"][name] = False
            st["svc_fail"][name] = 0
        else:
            st["svc_fail"][name] = st["svc_fail"].get(name, 0) + 1
            if not st["svc_down"].get(name) and \
                    st["svc_fail"][name] >= SVC_DOWN_AFTER:
                st["svc_down"][name] = True
                notify(f"\U0001f534 {host} service {name} DOWN",
                       f"{name} is not running.",
                       priority="high", tags="rotating_light")
    # Services that vanished from the report (container removed on purpose):
    # forget silently, never alert.
    for name in list(st["svc_down"]):
        if name not in seen:
            st["svc_down"].pop(name, None)
            st["svc_fail"].pop(name, None)
```

### Step 4 — unit + docs

`dashboard/systemd/dashboard-alerter.service` — extend the commented tunables:

```ini
# Environment=ALERT_DISK_HIGH=90
# Environment=ALERT_DISK_CLEAR=85
# Environment=ALERT_SVC_AFTER=2
```

README Alerts section: add disk-full and service-down to the bullet list of
conditions, list the new tunables, and note state now persists across alerter
restarts in `~/dashboard/alerter-state.json`.

`.gitignore`: add `dashboard/alerter-state.json` under "Agent runtime data".

### Step 5 — tests

Create `dashboard/tests/test_alerter.py`. Import trick + fake transport:

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import alerter  # noqa: E402


def run_poll(monkeypatch, tmp_path, payloads, prior_state=None):
    """Run alerter.check once per (host, payload) with notify captured."""
    sent = []
    monkeypatch.setattr(alerter, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(alerter, "notify",
                        lambda title, msg, **kw: sent.append(title))
    alerter.state.clear()
    alerter.state.update(prior_state or {})
    for host, payload in payloads:
        if payload is None:
            monkeypatch.setattr(alerter, "_fetch",
                                lambda url: (_ for _ in ()).throw(OSError()))
        else:
            monkeypatch.setattr(alerter, "_fetch",
                                lambda url, p=payload: p)
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


def test_service_down_debounced(monkeypatch, tmp_path):
    down = dict(BASE, containers=[{"name": "pihole", "status": "stopped"}])
    up = dict(BASE, containers=[{"name": "pihole", "status": "running"}])
    sent = run_poll(monkeypatch, tmp_path, [("scout", down)])
    assert not any("DOWN" in t for t in sent)          # 1 poll: debounced
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
```

Also add `_save_state()`/`_load_state()` round-trip and corrupt-file tests:
write `"{"` to the state file, assert `_load_state() == {}`.

## Edge cases a weaker model would miss

- **Debounce services, don't alert on the first stopped poll.** `update.sh`
  restarts services on every deploy and containers restart during image
  pulls; `SVC_DOWN_AFTER=2` (two consecutive 60s polls) is the difference
  between a useful alert and spam.
- **Removed services must be forgotten silently.** If you `docker rm` a
  container while it's in the `svc_down` state, a naive implementation sends
  "back UP" (wrong) or alerts forever (worse). Test 3 pins this.
- **Skip all data-dependent checks when the node is offline** — the existing
  code already `return`s from the fetch-failure branch; keep the new checks
  *after* that so a dead node doesn't also spew disk/service noise.
- **Atomic state writes** (`tmp` + `os.replace`), or a poorly-timed deploy
  restart leaves a truncated JSON file that a naive loader crashes on forever.
  `_load_state()` must swallow any parse error and start fresh.
- **State file keys arrive as strings from JSON** — that's fine here (all keys
  are host/service names), but don't get clever with non-string keys.
- **`extra_disks` may be absent** in payloads from older agents (`or []`
  everywhere).
- **Temperature `None`** is already handled; disk `percent` can also be
  missing on a partial payload — the `if pct is None: continue` guard is
  load-bearing.
- **Don't reset `svc_fail` on fetch errors** — offline handling owns that
  path; service counters should simply not advance while the node is
  unreachable.
- The persisted-state upgrade path: old state files (pre-change) lack the new
  keys; `st.setdefault(...)` at the top of `check()` handles them. Don't
  version the file.

## Acceptance criteria

1. `pytest dashboard/tests/test_alerter.py -q` passes.
2. On bastion: fill-simulation — temporarily set
   `Environment=ALERT_DISK_HIGH=10` in the unit, restart, get a 💾 alert
   within one poll; remove the override, restart, get the clear alert.
3. `docker stop <some container>` on a node → "service DOWN" alert after ~2
   polls; `docker start` → "back UP".
4. `sudo systemctl restart dashboard-alerter` while a node is hot/offline does
   **not** re-send the alert (state persisted in
   `~/dashboard/alerter-state.json`).
5. `journalctl -u dashboard-alerter -f` shows the startup banner including the
   configured disk/service thresholds (extend the existing banner print).
