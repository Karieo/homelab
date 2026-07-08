# PLAN: Lock down the agent's WiFi-config endpoints

**Rank: 2 of 5 — do after PLAN-ci-safe-deploys.**

## Goal

The agent binds `0.0.0.0:9090` with CORS `*` and **no authentication**. The
README's "config is on-site only" rule is enforced **only in the dashboard's
JavaScript** (`LOCAL_MODE` in `index.html`) — the HTTP endpoints themselves
accept a POST from anyone who can reach port 9090. Scout is a *travel router*:
on a hotel/airport network, every stranger on the hotel LAN can reach scout's
uplink IP and call:

- `POST /wifi/connect` — detach scout from the network (or join it to a rogue AP),
- `POST /wifi/repeater` — reconfigure/replace the AP,
- `POST /wifi/block` / `/wifi/block-unknown` — knock your own devices offline,
- `POST /wifi/stop` — kill the AP.

Fix: enforce, **server-side**, that mutating endpoints only accept requests
from trusted source networks — loopback, the AP subnet, and Tailscale — with an
env knob to extend the list. Read-only endpoints (`/stats`, `/history`,
`/health`, `/wifi/status`) stay open so the dashboard keeps working everywhere.

## Files to touch

| File | Action |
|------|--------|
| `dashboard/agent.py` | **modify** (trusted-source gate + apply to 6 routes) |
| `dashboard/tests/test_agent.py` | **modify** (add gate tests) |
| `dashboard/systemd/dashboard-agent.service` | **modify** (commented `TRUSTED_EXTRA_CIDRS` example) |
| `README.md` | **modify** (replace the "unauthenticated" security warning) |

## Step-by-step

### Step 1 — trusted-source helper in `agent.py`

Add `import functools` and `import ipaddress` to the imports (keep the import
block alphabetized).

Below the `KNOWN_DEVICES` block (around line 163), add:

```python
# ---- Trusted-source gate for mutating endpoints -------------------------
# The WiFi config/block endpoints reconfigure the node's networking, so they
# only accept requests from networks we control: loopback, the repeater AP
# subnet, and Tailscale. On a hotel/airport uplink the rest of the hotel LAN
# can reach port 9090 — those callers get a 403. Extend with a comma-separated
# TRUSTED_EXTRA_CIDRS env var (e.g. your home LAN for on-site raw-IP use).
_TRUSTED_CIDRS = [
    "127.0.0.0/8",
    "::1/128",
    os.environ.get("REPEATER_AP_SUBNET", "10.42.0.0/24"),
    "100.64.0.0/10",          # Tailscale IPv4 (CGNAT range)
    "fd7a:115c:a1e0::/48",    # Tailscale IPv6
]
_TRUSTED_CIDRS += [
    c.strip()
    for c in os.environ.get("TRUSTED_EXTRA_CIDRS", "").split(",")
    if c.strip()
]
_TRUSTED_NETS = []
for _c in _TRUSTED_CIDRS:
    try:
        _TRUSTED_NETS.append(ipaddress.ip_network(_c, strict=False))
    except ValueError:
        pass  # ignore a malformed extra CIDR rather than crash the agent


def _client_ip():
    addr = request.remote_addr or ""
    # The dev server reports IPv4 clients on a dual-stack socket as
    # IPv4-mapped IPv6 ("::ffff:1.2.3.4"); strip to the real address.
    if addr.startswith("::ffff:"):
        addr = addr[len("::ffff:"):]
    return addr


def _trusted_request():
    try:
        ip = ipaddress.ip_address(_client_ip())
    except ValueError:
        return False
    return any(ip in net for net in _TRUSTED_NETS)


def require_trusted_source(fn):
    """403 mutating requests from untrusted networks. OPTIONS passes through
    so CORS preflight still works and the browser can read our error JSON."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if request.method != "OPTIONS" and not _trusted_request():
            return jsonify({
                "ok": False,
                "error": "WiFi config is only allowed from the AP subnet, "
                         "Tailscale, or the node itself (your address: "
                         f"{_client_ip() or 'unknown'}). Set "
                         "TRUSTED_EXTRA_CIDRS in the agent unit to allow "
                         "another network.",
            }), 403
        return fn(*args, **kwargs)
    return wrapper
```

### Step 2 — apply to all six mutating routes

Insert `@require_trusted_source` **between** the `@app.route(...)` decorator
and the function `def` line for exactly these routes:

- `wifi_connect_route` (`/wifi/connect`)
- `wifi_repeater_route` (`/wifi/repeater`)
- `wifi_stop_route` (`/wifi/stop`)
- `wifi_block_route` (`/wifi/block`)
- `wifi_unblock_route` (`/wifi/unblock`)
- `wifi_block_unknown_route` (`/wifi/block-unknown`)

Do **not** add it to `/stats`, `/history`, `/health`, or `/wifi/status`.

### Step 3 — config surface

In `dashboard/systemd/dashboard-agent.service`, add to the commented options:

```ini
# Allow WiFi config from an extra network (default: AP subnet + Tailscale + localhost):
# Environment=TRUSTED_EXTRA_CIDRS=192.168.1.0/24
```

In `README.md`, rewrite the `⚠️ Security` blockquote in the WiFi section: the
mutating endpoints now reject requests whose source IP is outside
loopback / `10.42.0.0/24` (or `REPEATER_AP_SUBNET`) / Tailscale /
`TRUSTED_EXTRA_CIDRS`; note that the read-only endpoints and dashboard remain
unauthenticated and Tailscale-only access is still the intended posture, and
that a PSK is still briefly visible in the process list during `nmcli` runs.

### Step 4 — tests

Append to `dashboard/tests/test_agent.py`:

```python
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


def test_wifi_gate_preflight_passes():
    c = client()
    res = c.open("/wifi/connect", method="OPTIONS",
                 environ_base={"REMOTE_ADDR": "203.0.113.9"})
    assert res.status_code == 204
```

## Edge cases a weaker model would miss

- **`functools.wraps` is mandatory.** Without it every wrapped view function
  is named `wrapper` and Flask raises
  `View function mapping is overwriting an existing endpoint` at import time —
  the agent won't even start.
- **Decorator order matters.** `@app.route` must be *above*
  `@require_trusted_source`, otherwise the route registers the unwrapped
  function.
- **OPTIONS must pass through the gate.** If the CORS preflight is 403'd, the
  browser surfaces an opaque network error and the dashboard can't display the
  real "your address is untrusted" message from the POST.
- **Do NOT try to allow "all private ranges".** Hotel LANs are RFC1918 too —
  allowing `192.168.0.0/16`/`10.0.0.0/8` wholesale defeats the whole fix. Only
  the specific AP subnet default plus the explicit env override.
- **IPv4-mapped IPv6 (`::ffff:a.b.c.d`)** — without the strip, every IPv4
  client fails the check when the socket is dual-stack, which looks like
  "the gate blocks everything" and tempts a weaker model to delete the gate.
- **Behavior change to document:** using the on-site dashboard from a home-LAN
  raw IP (e.g. `http://192.168.1.20:9091`) now gets 403 on config actions until
  `TRUSTED_EXTRA_CIDRS=192.168.1.0/24` is set in the agent unit. The primary
  on-site flows (phone on the scout AP at `10.42.0.1`, or via Tailscale) keep
  working with zero config.
- **Malformed `TRUSTED_EXTRA_CIDRS` entries must be ignored, not fatal** — the
  agent starting is more important than a typo'd CIDR.
- The gate reads `request.remote_addr` directly. That's correct here because
  the agent is never behind a reverse proxy; do **not** honor
  `X-Forwarded-For` (it would let any attacker spoof a trusted source).

## Acceptance criteria

1. All new tests pass: `pytest dashboard/tests -q`.
2. Agent starts cleanly: `python3 -c "import sys; sys.path.insert(0,'dashboard'); import agent"` exits 0.
3. On scout (or any node with the agent), from a machine on an *untrusted*
   network path:
   `curl -s -o /dev/null -w '%{http_code}' -X POST http://<scout-uplink-ip>:9090/wifi/stop` → `403`,
   and the AP stays up.
4. From a phone browser on the scout AP (`http://10.42.0.1:9091`): Block /
   Unblock buttons and the repeater form still work.
5. From the networked dashboard over Tailscale, everything renders as before
   (read-only WiFi panel unaffected).
6. `curl http://localhost:9090/stats` on the node still returns 200 (read
   endpoints unauthenticated).
