# PLAN: Background sampler — continuous history + fast cached /stats

**Rank: 3 of 5 — do after PLAN-secure-wifi-endpoints. Touches the same
`stats()` region as PLAN-meshtastic-panel, so land this one first.**

## Goal

Two design flaws in `dashboard/agent.py`, one fix:

1. **History only records while someone is watching.** `record_sample()` is
   called inside the `/stats` request handler, so the "24H TRENDS" sparklines
   have gaps for every period the dashboard tab wasn't open and the alerter
   wasn't running. The feature silently under-delivers exactly when you want
   it (what happened overnight?).
2. **`/stats` is slow and heavy.** Each poll runs
   `psutil.cpu_percent(interval=1)` (blocks a full second) plus ~10 serial
   subprocess calls (`docker ps`, `systemctl`, `tailscale`, `nmcli` ×3,
   `iw`, `ip` ×2), each with a 10s timeout. The dashboard *and* the alerter
   both poll, doubling the load, and one hung `nmcli` stalls the whole
   response.

Fix: move all collection into a **background sampler thread** that builds the
full payload every 30 seconds, records the history sample, and caches the
result. `/stats` becomes a cache read that returns in milliseconds, and
history is continuous from boot regardless of who's polling.

## Files to touch

| File | Action |
|------|--------|
| `dashboard/agent.py` | **modify** (extract `build_payload()`, add sampler thread + cache, slim `/stats`) |
| `dashboard/tests/test_agent.py` | **modify** (payload + cache tests) |
| `README.md` | **modify** (one sentence: history records continuously; stats are ≤30s fresh) |

## Step-by-step

### Step 1 — extract `build_payload()`

In `agent.py`, create a new function directly above the `@app.route("/stats")`
handler containing the current body of `stats()` with these changes:

```python
SAMPLE_INTERVAL_SEC = 30      # near the other constants at the top
_stats_cache_lock = threading.Lock()
_stats_cache = {"built": 0.0, "payload": None}


def build_payload():
    """Collect everything /stats reports. Slow (subprocesses + 1s CPU
    sample) — called by the background sampler, not per-request."""
    hostname = socket.gethostname()
    disk = psutil.disk_usage("/")
    ram = psutil.virtual_memory()
    cpu_percent = psutil.cpu_percent(interval=1)
    temp = get_cpu_temp()

    network = get_tailscale_status()
    network.update(get_network_throughput())

    payload = { ... }          # identical dict to today's stats() body

    # keep the pihole / jellyfin / remndrs / wifi conditional blocks
    # exactly as they are today, appending to `payload`

    return payload, (cpu_percent, temp, ram.percent, disk.percent)
```

Note the return signature: the payload **and** the 4-tuple used for history
recording. `record_sample(...)` is **removed** from the request path entirely.

### Step 2 — sampler thread + cache

Below `build_payload()`:

```python
def _refresh_stats_cache():
    payload, sample = build_payload()
    with _stats_cache_lock:
        _stats_cache["payload"] = payload
        _stats_cache["built"] = time.time()
    record_sample(*sample)
    return payload


def _sampler_loop():
    while True:
        try:
            _refresh_stats_cache()
        except Exception:
            pass  # never let one bad tick kill sampling
        time.sleep(SAMPLE_INTERVAL_SEC)
```

### Step 3 — slim the `/stats` route

```python
@app.route("/stats")
def stats():
    with _stats_cache_lock:
        payload = _stats_cache["payload"]
        age = time.time() - _stats_cache["built"]
    # Serve the cache; rebuild synchronously only if the sampler hasn't
    # produced anything yet (first seconds after boot) or has wedged.
    if payload is None or age > SAMPLE_INTERVAL_SEC * 3:
        payload = _refresh_stats_cache()
    return jsonify(payload)
```

### Step 4 — startup wiring

At the bottom of the module (where `init_db()` etc. run):

```python
init_db()
reapply_blocklist()
psutil.cpu_percent(interval=None)   # prime the CPU counter
threading.Thread(target=_sampler_loop, daemon=True).start()
```

Keep the existing curfew-thread startup as-is.

### Step 5 — tests

Append to `dashboard/tests/test_agent.py`:

```python
def test_build_payload_shape():
    payload, sample = agent.build_payload()
    assert payload["hostname"]
    assert len(sample) == 4


def test_stats_served_from_cache():
    c = client()
    a = c.get("/stats").get_json()
    b = c.get("/stats").get_json()
    # Two immediate polls must be the same cached build (identical build
    # timestamp), proving no per-request collection happens.
    assert a["timestamp"] == b["timestamp"]
```

### Step 6 — README

In the paragraph describing `GET /history`, note that samples are recorded by
the agent itself every 30s from boot (previously only while the dashboard was
polling), and that `/stats` values are cached and at most ~30s old.

## Edge cases a weaker model would miss

- **`psutil.cpu_percent(interval=None)` returns garbage (0.0) on its first
  call** — that's why Step 4 primes it once at startup. Inside
  `build_payload()` keep `interval=1` (a 1s blocking sample is fine in a
  background thread and keeps the number meaningful even when the fallback
  synchronous rebuild path runs).
- **Network throughput math depends on call spacing.**
  `get_network_throughput()` averages since its *previous* call. With the
  sampler as the only caller it becomes a clean 30s average. Do not also call
  it from the route, or concurrent pollers will produce nonsense (tiny dt →
  spiky rates). Same reasoning is why `record_sample` must have exactly one
  call site.
- **First-request race:** systemd starts the agent and the alerter may hit
  `/stats` before the first sampler tick finishes (the tick itself takes
  ~1-3s). The `payload is None → rebuild synchronously` branch covers it;
  don't return 503.
- **The cache must be replaced, not mutated.** Assign a fresh dict each tick
  under the lock; never `payload.update(...)` on the object already handed to
  Flask threads.
- **Flask's dev server is threaded by default** — the lock around read/write
  of the two cache fields is required, not decorative.
- **Do not start the sampler under `if __name__ == "__main__"` only.** Tests
  and any WSGI wrapper import the module; module-level startup (matching how
  `init_db()` already works) keeps behavior identical everywhere. The thread
  is `daemon=True` so pytest still exits.
- **Don't enable Flask debug/reloader** (it isn't today — keep it that way):
  the reloader would fork a second process and double the sampler.
- **`stats.db` write contention:** `record_sample` now runs in the sampler
  thread while `/history` reads from request threads. SQLite handles this via
  the existing per-call connections and `timeout=5`; do not hold one global
  connection.
- **The WiFi status shown on the on-site panel now lags up to 30s.** That's
  acceptable because every WiFi POST action already returns a live
  `get_wifi_status()` in its response (`data.status`), which the UI applies
  immediately. Don't "fix" the lag by making `/stats` live again.
- **The alerter is unaffected** — offline detection is connection-level, temp
  values being ≤30s stale doesn't matter at a 60s poll interval.

## Acceptance criteria

1. `pytest dashboard/tests -q` passes (including both new tests).
2. On a node: `time curl -s localhost:9090/stats >/dev/null` completes in
   **< 0.3s** (was > 1s).
3. Close all dashboard tabs, stop the alerter for 2+ hours, reopen the
   dashboard: the 24H TRENDS sparklines show **no gap** for that window
   (verify via `sqlite3 ~/dashboard/stats.db "SELECT COUNT(*) FROM samples
   WHERE ts > strftime('%s','now') - 3600"` → ≈120 rows/hour).
4. `journalctl -u dashboard-agent` shows no repeated tracebacks from the
   sampler loop.
5. WiFi connect/repeater/block actions from the on-site panel still show
   fresh status immediately after each action.
