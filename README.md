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

- **`dashboard/alerter.py`** — optional watcher (runs on bastion) that polls each
  node and pushes a notification when a node goes offline or CPU temp crosses a
  threshold. See [Alerts](#alerts).

The agent also exposes **`GET /history?hours=24`** — downsampled CPU/temp/RAM/disk
samples logged to a local SQLite file (`dashboard/stats.db`, gitignored, 7-day
retention) and drawn as 24h sparklines under each node. `GET /stats` additionally
reports live network throughput (`rx_bytes_sec`/`tx_bytes_sec`), extra mounted
disks, and—where configured—a `pihole` summary, `jellyfin` now-playing list, and
`remndrs_open` count.

### Auto-update (hands-off deploys)

So you don't have to copy files to every node by hand, install the auto-updater on
each node you want to self-update:

```bash
cd ~/homelab/dashboard && ./install-updater.sh
```

It adds a systemd timer (`dashboard-update.timer`, every ~15 min) that runs
`update.sh`: fetch the tracked branch (`main`), and if there are new commits,
fast-forward the repo, re-stage `agent.py` / `index.html` / `alerter.py` into
`~/dashboard`, and restart whichever dashboard services are installed on that node.
After this, **deploying is just merging to `main`** — each node picks it up on its
next tick. Run `update.sh --force` (or `sudo systemctl start dashboard-update`) to
update immediately.

Notes:
- It's **fast-forward only** and never discards local changes, so keep per-node
  config committed to the repo. Secrets stay in the systemd unit env, not in files.
- `git pull` must work non-interactively on the node (stored credential helper, a
  PAT, or an SSH remote). Test once with `cd ~/homelab && git pull`.
- The clone is expected at `~/homelab`; override with `DASHBOARD_REPO=/path` (env in
  the unit) if yours lives elsewhere. The staged `~/dashboard/update.sh` locates the
  repo via this, not its own path, and re-stages itself each run.
- A scoped sudoers drop-in lets the timer restart only the three `dashboard-*`
  units without a password; everything else runs as your user.
- Logs: `journalctl -u dashboard-update.service -f` ·
  next run: `systemctl list-timers dashboard-update.timer`.
- **Deploy pings:** if a notification channel is configured (see below), each
  successful deploy posts `<host> deploy — updated <old> → <new> — <commit>` to
  Discord/ntfy (green), and a fast-forward failure posts a red alert.

#### Shared notification config (`notify.env`)

The alerter and the auto-updater both read an optional `~/dashboard/notify.env`
(gitignored) so you configure the channel once:

```bash
cat > ~/dashboard/notify.env <<'EOF'
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxx/yyy
# NTFY_URL=https://ntfy.sh/your-homelab-topic
EOF
sudo systemctl restart dashboard-alerter dashboard-update.timer
```

Both units pull it in via `EnvironmentFile=-`, and `update.sh` also sources it for
manual runs. (Inline `Environment=` lines in a unit still work and override it.)

### Extra disks

Report a mounted drive beyond root (e.g. the Samsung T7) as its own usage row.
Edit `EXTRA_DISKS` in `agent.py`:

```python
EXTRA_DISKS = {"bastion": [{"name": "T7", "path": "/mnt/t7"}]}
```

Unmounted/missing paths are skipped silently, so it's safe to list a drive before
it's plugged in.

### Jellyfin now-playing

On the node running Jellyfin, set an API key (Jellyfin → Dashboard → API Keys) so
the agent can read active sessions, then restart the agent:

```ini
Environment=JELLYFIN_API_KEY=your-api-key
# Environment=JELLYFIN_BASE_URL=http://localhost:8096
```

Host selected via the `JELLYFIN` map in `agent.py` (defaults to `bastion`).

### Remndrs open count

Show your open-reminder count. Point the agent at the self-hosted Remndrs count
endpoint via env (host selected via the `REMNDRS` map, defaults to `bastion`):

```ini
Environment=REMNDRS_COUNT_URL=http://localhost:3000/api/reminders/open/count
# Environment=REMNDRS_COUNT_FIELD=count   # JSON field to read (or returns a bare number / array)
# Environment=REMNDRS_TOKEN=your-token    # sent as a Bearer header if set
```

### Alerts

Install the alerter on **bastion** (it polls all nodes):

```bash
./install-alerter.sh
```

It logs to the journal until you set a notification channel. Configure **Discord**
and/or **ntfy** (it sends to every channel set), then restart:

```bash
sudo systemctl edit --full dashboard-alerter
#   Environment=DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxx/yyy
#   Environment=NTFY_URL=https://ntfy.sh/your-homelab-topic
sudo systemctl restart dashboard-alerter
```

For Discord, create the webhook in the target channel: **Server Settings →
Integrations → Webhooks → New Webhook → Copy Webhook URL**. Alerts arrive as
colored embeds (red for offline / hot, green for recovered).

Alerts fire on transitions only (offline ⇄ online, temp high ⇄ cleared) with
hysteresis, so a persistently-hot node won't spam. Tunables: `ALERT_TEMP_HIGH`
(default 80°C), `ALERT_TEMP_CLEAR` (72°C), `ALERT_POLL_SEC` (60),
`ALERT_OFFLINE_AFTER` (2), `ALERT_NODES`.

### Pi-hole widget

On the node running Pi-hole, set the web/app password so the agent can query the
admin API (supports both Pi-hole v6 and v5, auto-detected). Add it to the agent's
systemd unit and restart:

```ini
Environment=PIHOLE_PASSWORD=your-pihole-password
# Environment=PIHOLE_BASE_URL=http://localhost   # if Pi-hole isn't on localhost:80
```

The host is selected via the `PIHOLE` map in `agent.py` (defaults to `scout`).

### WiFi setup panel

On any node with a wireless interface (`wlan0`), the dashboard shows a **WiFi
Setup** panel with two modes:

- **Client** — connect `wlan0` to a network. Fields: SSID, optional **Username**
  (for WPA-Enterprise / 802.1x, PEAP+MSCHAPv2), Password, and a "Clone MAC"
  checkbox (pre-filled with `80:B9:89:90:7C:CA`). `POST /wifi/connect`.
- **Repeater** (travel router) — join an upstream WiFi on `wlan0` and re-broadcast
  it as your own private network on a second radio (`wlan1`, typically a USB
  adapter) with NAT. Downstream devices sit behind the AP, so they never hit the
  upstream's captive portal once the upstream is up. Fields: upstream SSID /
  username / password + Clone MAC, and broadcast SSID / password (8-63 chars).
  `POST /wifi/repeater`, with `POST /wifi/stop` to drop the AP.

The panel shows live status for both radios (client SSID · IP · signal, and AP
SSID · client count). `GET /wifi/status` returns the same. Interfaces are
configurable via `WIFI_IFACE` / `WIFI_AP_IFACE`.

The agent runs `nmcli` (and `iw`) via passwordless sudo. `install-agent.sh` sets
up the scoped sudoers drop-in automatically when `wlan0` + `nmcli` are present. For
a node that's already installed (auto-update only copies files), add it once:

```bash
echo "$USER ALL=(root) NOPASSWD: $(command -v nmcli), $(command -v iw)" \
  | sudo tee /etc/sudoers.d/dashboard-nmcli && sudo chmod 0440 /etc/sudoers.d/dashboard-nmcli
sudo systemctl restart dashboard-agent
```

> ⚠️ **Security:** these endpoints are unauthenticated (like the rest of the
> dashboard) and reconfigure the node's networking. Intended for Tailscale-only
> access. Credentials are passed to `nmcli` as argv (no shell injection), but a
> PSK/password is briefly visible in the node's process list while connecting.

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
