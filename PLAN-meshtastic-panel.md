# PLAN: Meshtastic panel — surface the mesh node on the dashboard

**Rank: 5 of 5 — do last. CLAUDE.md's "Next up: finish the Meshtastic node"
starts here: make the radio observable, so hardware work stops being blind.
If PLAN-background-sampler has landed, integrate at the point it moved the
payload build to (noted below); this plan works either way.**

## Goal

Scout carries a Meshtastic radio (served by `meshtasticd` / the Meshtastic
Python API over TCP, default port 4403). Nothing in the repo reports it: you
can't see from the dashboard whether the radio is even alive, how many nodes
the mesh sees, battery level, or channel utilization. Add:

1. An agent-side collector (`meshtastic` key in `/stats`) — node count,
   battery, channel/air utilization, last-heard time — fully optional and
   inert on nodes without a radio.
2. A dashboard widget (like the Pi-hole one) rendering those numbers.
3. A `meshtasticd` service row on scout's SERVICES list, only when the unit
   actually exists.

## Files to touch

| File | Action |
|------|--------|
| `dashboard/agent.py` | **modify** (MESHTASTIC config map + collector + payload wiring + optional-service skip) |
| `dashboard/index.html` | **modify** (`meshtasticBlock()` widget) |
| `dashboard/tests/test_agent.py` | **modify** (collector disabled-path tests) |
| `dashboard/systemd/dashboard-agent.service` | **modify** (commented env examples) |
| `README.md` | **modify** (new "Meshtastic widget" section) |

The `meshtastic` Python package is **not** added to `requirements.txt` — it is
heavy and only scout needs it. It's an opt-in install, documented in README.

## Step-by-step

### Step 1 — config map in `agent.py`

Next to the `PIHOLE` / `JELLYFIN` maps:

```python
# Meshtastic radio status, per host. Talks to meshtasticd (or any node
# reachable over the Meshtastic TCP API, port 4403) via the `meshtastic`
# Python package — which is optional; without it (or with the API down) the
# widget simply doesn't render. Enable by installing the package on the node:
#   pip3 install --break-system-packages meshtastic
MESHTASTIC = {
    "scout": {
        "host": os.environ.get("MESHTASTIC_HOST", "localhost"),
    },
}
MESHTASTIC_SAMPLE_SEC = 60  # radio polls are expensive; cache this long
_mesh_cache = {"ts": 0.0, "data": None}
_mesh_lock = threading.Lock()
```

### Step 2 — collector

New section after the Remndrs collector:

```python
# ---- Meshtastic radio status -------------------------------------------

def _mesh_fetch(host):
    """Open the Meshtastic TCP API, read node DB + own metrics, close."""
    from meshtastic.tcp_interface import TCPInterface  # lazy: optional dep

    iface = TCPInterface(hostname=host)
    try:
        my = iface.getMyNodeInfo() or {}
        nodes = dict(iface.nodes or {})
    finally:
        iface.close()

    metrics = my.get("deviceMetrics") or {}
    my_num = my.get("num")
    last_heard = None
    for n in nodes.values():
        if n.get("num") == my_num:
            continue
        lh = n.get("lastHeard")
        if lh and (last_heard is None or lh > last_heard):
            last_heard = lh
    return {
        "nodes": len(nodes),
        "battery": metrics.get("batteryLevel"),        # 101 == on USB power
        "voltage": metrics.get("voltage"),
        "channel_util": metrics.get("channelUtilization"),
        "air_util_tx": metrics.get("airUtilTx"),
        "last_heard": last_heard,                      # epoch secs, other nodes
    }


def get_meshtastic_status(hostname):
    """Cached mesh status, or None when unconfigured/unavailable."""
    cfg = MESHTASTIC.get(hostname)
    if not cfg:
        return None
    with _mesh_lock:
        if time.time() - _mesh_cache["ts"] < MESHTASTIC_SAMPLE_SEC:
            return _mesh_cache["data"]
    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            data = ex.submit(_mesh_fetch, cfg["host"]).result(timeout=15)
    except Exception:
        data = None
    with _mesh_lock:
        _mesh_cache["ts"] = time.time()
        _mesh_cache["data"] = data
    return data
```

### Step 3 — payload wiring

Where the `pihole`/`jellyfin`/`remndrs` blocks append to the payload — inside
`stats()` today, or inside `build_payload()` if PLAN-background-sampler has
landed — add:

```python
    mesh = get_meshtastic_status(hostname)
    if mesh is not None:
        payload["meshtastic"] = mesh
```

### Step 4 — optional service row

In `EXTRA_SERVICES`, extend scout:

```python
    "scout": [
        {"name": "pihole", "unit": "pihole-FTL.service", "port": 80},
        {"name": "kiwix", "unit": "kiwix.service", "port": 8090},
        {"name": "meshtastic", "unit": "meshtasticd.service", "port": None,
         "optional": True},
    ],
```

And in `get_extra_services()`, skip optional services whose unit isn't
installed (so a scout without meshtasticd doesn't show a bogus red dot):

```python
def _unit_exists(unit):
    try:
        result = subprocess.run(
            ["systemctl", "list-unit-files", unit, "--no-legend"],
            capture_output=True, text=True, timeout=10,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


# inside get_extra_services(), first line of the loop body:
        if svc.get("optional") and not _unit_exists(svc["unit"]):
            continue
```

### Step 5 — dashboard widget in `index.html`

Add after the `piholeBlock` function:

```javascript
  // ---- Meshtastic widget ---------------------------------------------
  function fmtAgo(epoch) {
    if (epoch == null) return "—";
    const s = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }

  function meshtasticBlock(m) {
    if (!m) return "";
    const batt = m.battery == null ? "—"
      : m.battery > 100 ? "PWR" : Math.round(m.battery) + "%";
    const util = m.channel_util == null ? "—"
      : m.channel_util.toFixed(1) + "%";
    return `
      <div class="pihole">
        <div class="pihole-head">MESHTASTIC</div>
        <div class="pihole-stats">
          <div class="ph-stat"><span class="ph-num">${fmtNum(m.nodes)}</span><span class="ph-lbl">nodes</span></div>
          <div class="ph-stat"><span class="ph-num">${esc(batt)}</span><span class="ph-lbl">battery</span></div>
          <div class="ph-stat"><span class="ph-num">${esc(util)}</span><span class="ph-lbl">ch util</span></div>
          <div class="ph-stat"><span class="ph-num">${esc(fmtAgo(m.last_heard))}</span><span class="ph-lbl">last heard</span></div>
        </div>
      </div>`;
  }
```

(It reuses the `.pihole` CSS classes on purpose — same visual language, no new
CSS.) In `renderPanel()`, add `${meshtasticBlock(data.meshtastic)}` on the
line directly after `${piholeBlock(data.pihole)}`.

### Step 6 — config & docs

`dashboard/systemd/dashboard-agent.service` commented options:

```ini
# Meshtastic widget (node running meshtasticd / radio with TCP API):
# Environment=MESHTASTIC_HOST=localhost
```

README: new "Meshtastic widget" subsection next to the Pi-hole widget section:
install `meshtastic` via pip (needs `--break-system-packages` on Bookworm),
host selected via the `MESHTASTIC` map (defaults to `scout`), reads over the
TCP API (port 4403) so it works with `meshtasticd` or a WiFi-attached radio,
widget hides itself when the radio/API is down.

### Step 7 — tests

```python
def test_meshtastic_unconfigured_host_returns_none():
    assert agent.get_meshtastic_status("not-a-configured-host") is None


def test_meshtastic_unavailable_is_silently_none(monkeypatch):
    # Configured host but no radio/package on the CI runner: must return
    # None quickly and never raise.
    agent.MESHTASTIC["testhost"] = {"host": "127.0.0.1"}
    try:
        agent._mesh_cache["ts"] = 0
        assert agent.get_meshtastic_status("testhost") is None
    finally:
        del agent.MESHTASTIC["testhost"]
```

And assert `/stats` still returns 200 with no `meshtastic` key on the runner.

## Edge cases a weaker model would miss

- **The `meshtastic` import must be lazy** (inside `_mesh_fetch`). A top-level
  import makes the agent crash-loop on every node that didn't opt in — and the
  auto-updater ships `agent.py` to *all* nodes.
- **`TCPInterface` can hang, not just fail.** A half-up radio or firewalled
  port can block far longer than a request cycle; that's what the
  executor-with-`timeout=15` watchdog is for. On timeout the worker thread is
  abandoned (daemonized by pool shutdown at process exit) — acceptable for a
  homelab agent, and the 60s cache stops it from piling up a thread per poll.
- **Always `iface.close()` in a `finally`** — TCPInterface spawns reader
  threads and holds a socket; leaking one per sample exhausts fds within a
  day.
- **`batteryLevel == 101` means "on external power"**, not a bug — render it
  as `PWR`, don't clamp to 100%.
- **Exclude self from `last_heard`** — your own node is always "just heard",
  which would make the stat permanently lie.
- **Cache failures too.** `get_meshtastic_status` stores `data = None` with a
  fresh timestamp on failure; otherwise a dead radio re-runs the 15s timeout
  on every single `/stats` poll and drags the whole payload build.
- **`_unit_exists` vs `is-active`:** `systemctl is-active` on a nonexistent
  unit returns "inactive" — the current code would show a red "stopped" dot on
  every scout without meshtasticd. The `optional` flag + `list-unit-files`
  check is required, and existing non-optional entries must keep today's
  behavior exactly (only add the skip for `optional`).
- **Widget hiding:** the key is absent from `/stats` when unavailable, and
  `meshtasticBlock(undefined)` returns `""` — verify both halves; don't render
  an empty box with dashes on nodes without a radio.
- **Ordering with PLAN-background-sampler:** if the sampler plan landed, the
  collector runs inside the sampler thread — the 60s internal cache and the
  30s sampler tick compose fine (radio queried every other tick). If it
  hasn't, the cache is what protects the request path. Either way, do not
  call `get_meshtastic_status` anywhere else.
- **`fmtAgo` uses browser clock vs node epoch** — mesh `lastHeard` comes from
  the radio's RTC; clamp negatives to 0 (done via `Math.max`) so a skewed
  clock shows "0s ago" instead of "-42s ago".

## Acceptance criteria

1. `pytest dashboard/tests -q` passes; `/stats` on a radio-less machine has no
   `meshtastic` key and no added latency.
2. On scout with `meshtastic` installed and the radio up:
   `curl -s localhost:9090/stats | python3 -m json.tool` shows a `meshtastic`
   object with plausible `nodes` (≥1), `battery`, `channel_util`,
   `last_heard`.
3. Dashboard shows a MESHTASTIC box under scout's Pi-hole box with all four
   stats; battery shows `PWR` when the node is USB-powered.
4. Stop `meshtasticd` on scout: within ~60s the widget disappears from the
   dashboard (no error box), `/stats` still 200s fast, and the SERVICES list
   shows `meshtastic` with a red dot (unit exists but inactive).
5. On a hypothetical scout **without** `meshtasticd.service` installed, the
   SERVICES list shows no `meshtastic` row at all.
