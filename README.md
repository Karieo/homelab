# homelab

Personal homelab tooling.

## Ops Dashboard

A single-page, live ops dashboard for the homelab nodes (**bastion** + **scout**).
Dark, terminal-adjacent aesthetic — mission control, not a SaaS app. Shows live
hardware stats, service health, and Tailscale status for each node side by side,
auto-refreshing every 30 seconds.

```
┌─────────────────────────────────────────────┐
│  ◉ HOMELAB OPS              last updated 14s  │
├──────────────────┬──────────────────────────┤
│  BASTION         │  SCOUT                    │
│  Jetson TX2      │  Pi 4B                    │
│  ╭── 54°C ──╮    │  ╭── 44°C ──╮             │
│  CPU  ████░ 42%  │  CPU  ██░░░ 18%           │
│  RAM  ███░░ 2.1G │  RAM  █░░░░ 0.5G          │
│  DISK ██░░░ 18G  │  DISK ████░ 12G           │
│  SERVICES        │  SERVICES                 │
│  ● remndrs       │  ● pihole                 │
│  ● vaultwarden   │  ● kiwix                  │
│  …               │  …                        │
└──────────────────┴──────────────────────────┘
```

### Architecture

```
bastion:9090/stats  ◀── stats agent (Flask + psutil)
scout:9090/stats    ◀── stats agent (Flask + psutil)
        │
bastion:9091        ◀── dashboard (static HTML/CSS/JS)
        │
browser             ◀── polls every 30s via fetch()
```

- **`dashboard/agent.py`** — Flask stats agent. Runs on every node, exposes
  `GET /stats` (hardware, network, containers) and `GET /health`. Emits CORS
  headers so the cross-port dashboard can fetch it without a reverse proxy.
- **`dashboard/index.html`** — the dashboard. Vanilla HTML/CSS/JS, no build step.
  Polls each node, renders metric bars, an animated SVG CPU-temperature arc, and
  color-coded clickable service dots. Falls back to an `OFFLINE` panel when a node
  is unreachable.

### Deploy

On **every** node you want to monitor (bastion, scout, ...):

```bash
git clone <this repo> && cd homelab/dashboard
./install-agent.sh        # installs deps, agent.py, systemd unit, opens 9090
curl http://localhost:9090/stats | python3 -m json.tool   # verify
```

On **bastion** (serves the dashboard):

```bash
./install-dashboard.sh    # installs index.html, systemd unit, opens 9091
```

Then open **http://bastion:9091** from anywhere on Tailscale.

> The install scripts rewrite the `clay`/`/home/clay` placeholders in the systemd
> units to the current `$USER` and `$HOME` automatically.

### Configuration

- **Nodes shown:** edit the `ENDPOINTS` array at the top of the `<script>` block
  in `dashboard/index.html`.
- **Roles / known service ports:** edit `ROLES` and `KNOWN_PORTS` in
  `dashboard/agent.py`.
- **Native (non-Docker) services:** some nodes run services like Pi-hole or
  Kiwix as plain systemd units rather than containers. List them per host in
  `EXTRA_SERVICES` in `dashboard/agent.py` — the agent checks each with
  `systemctl is-active` and reports them alongside Docker containers.
- **Refresh interval / temp gauge range:** `REFRESH_MS`, `GAUGE_MIN`, `GAUGE_MAX`
  in `index.html`.

### Access points

| URL | What |
|-----|------|
| `http://bastion:9091`      | Main dashboard |
| `http://bastion:9090/stats`| Raw bastion JSON |
| `http://scout:9090/stats`  | Raw scout JSON |

### Notes

- No auth — this is intended to be reachable only over Tailscale.
- The agent uses Flask's development server, which is fine for a single-user
  LAN/Tailscale dashboard. Front it with a real WSGI server if you ever expose it.
