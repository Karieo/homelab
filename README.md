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
- **Repeater** (travel router) — re-broadcast `wlan0`'s connection as your own
  private network on a second radio (`wlan1`, typically a USB adapter) with NAT.
  Downstream devices sit behind the AP, so they never hit the upstream's captive
  portal. **"Keep current wlan0 connection" is on by default** (recommended): the
  repeater leaves `wlan0` untouched and only brings up the `wlan1` AP — ideal when
  `wlan0` is already on the network you want to repeat. Untick it to (re)connect
  `wlan0` to a different upstream first (SSID / username / password + Clone MAC).
  Broadcast SSID / password (8-63 chars). `POST /wifi/repeater`, `POST /wifi/stop`.

> 🛡️ **Lockout guard:** the agent **refuses to reconfigure `wlan0` when it's the
> node's only network path** (sole default route) — that would cut off remote
> access. Connect Ethernet first, use the repeater's "keep current connection"
> mode, or pass `force=true`. (`POST /wifi/connect` and reconfiguring-upstream
> repeater calls return `409` with `needs_force` in that case.)

The panel shows live status for both radios (client SSID · IP · signal, and AP
SSID · client count). `GET /wifi/status` returns the same. Interfaces are
configurable via `WIFI_IFACE` (upstream/client) and `WIFI_AP_IFACE` (broadcast).

> 🔐 **Config is on-site only.** The interactive WiFi controls (Client / Repeater)
> appear **only when the dashboard is opened from the node by raw IP** (on-site
> mode, e.g. `http://10.42.0.1:9091` on scout's AP). On the networked dashboard
> (`bastion:9091`) the same panel becomes **read-only** — it lists the devices
> connected to the node's AP (hostname · IP · signal · uptime, from `iw` + the
> AP's DHCP leases) but exposes no controls, so you can't reconfigure a remote
> node's WiFi from the main dashboard by accident.

> 📡 **Choosing radios:** the broadcast (`WIFI_AP_IFACE`) radio must support AP
> mode with WPA2. The Raspberry Pi's **built-in** (Broadcom `brcmfmac`) radio is
> reliable as an AP; many cheap USB dongles are **not** — notably the **RTL8821AU**
> (e.g. TP-Link Archer T2U Plus) beacons but fails client association in AP mode.
> If the AP shows up but devices can't join (or get "incorrect password" with the
> right password), use the built-in radio for the AP and the USB adapter as the
> upstream client — e.g. set `WIFI_IFACE=wlan1`, `WIFI_AP_IFACE=wlan0`. Adapters
> with MediaTek `mt7612u`/`mt7610u` chipsets make good APs.

#### On-site / offline access (no internet, no Tailscale)

The main dashboard lives on `bastion:9091`, but when a travel node (e.g. scout)
is somewhere new with **no upstream yet**, you can't reach bastion — and you need
the WiFi panel precisely to *get* online. Two pieces solve this chicken-and-egg:

- **Persistent management AP.** The repeater AP is created with
  `connection.autoconnect=yes`, so the node always self-broadcasts its AP at boot,
  independent of any upstream. Join it from your phone and the node is reachable at
  its AP gateway IP (NetworkManager's shared mode → `http://10.42.0.1`).
- **Self-hosted UI in offline mode.** Run `./install-dashboard.sh` on the travel
  node too, then open `http://10.42.0.1:9091`. When the dashboard is served from a
  **raw IP** (rather than a hostname), it talks only to the *local* agent on the
  same host, so it works with no internet, Tailscale, or MagicDNS. Use the WiFi
  panel's Client tab to join the upstream, then the rest of the tailnet comes back.

> 🏨 **Captive-portal upstreams (hotels, airports):** join the open SSID in the
> Client tab (blank password), then open any plain-`http://` page from a device on
> the node's AP — the portal intercepts it, and because all traffic is NAT'd out
> the node's uplink, completing the login authorizes the *node's* MAC. Every device
> behind it is then online through that single authorized connection.

> 🌐 **Repeater NAT on Docker/ufw hosts:** NetworkManager's shared-mode NAT can be
> overridden by ufw (FORWARD policy `DROP`) and Docker's firewall, leaving AP
> clients with an IP but no internet. `install-agent.sh` installs a NetworkManager
> dispatcher hook (`/etc/NetworkManager/dispatcher.d/90-dashboard-repeater-nat`)
> that adds the masquerade + forward rules for the AP subnet (`10.42.0.0/24`)
> whenever the AP comes up — including at boot.

#### Ad-blocking on the repeated network (Pi-hole as the AP resolver)

To filter DNS for every device on the AP (ScoutAP), Pi-hole must be the resolver
those clients use. The catch: NetworkManager's shared mode runs its **own**
dnsmasq on the AP gateway (`10.42.0.1:53`) for DHCP **and** DNS, which grabs port
53 before Pi-hole's FTL can — FTL then logs `failed to create listening socket
for port 53: Address already in use` and silently serves nothing, so Pi-hole sees
zero queries.

The fix is to make NetworkManager's dnsmasq **DHCP-only** and hand clients Pi-hole
as their DNS, leaving port 53 to FTL. On the AP node (e.g. scout):

```bash
# 1. AP does DHCP only; advertise Pi-hole (the gateway) as the DNS server.
sudo tee /etc/NetworkManager/dnsmasq-shared.d/01-pihole.conf >/dev/null <<'EOF'
port=0
dhcp-option=option:dns-server,10.42.0.1
EOF

# 2. Let FTL answer local subnets (AP + loopback). LOCAL binds 0.0.0.0 and
#    filters by subnet, so it survives reboots and interface changes — unlike
#    SINGLE/BIND, which need the AP up before FTL starts.
sudo pihole-FTL --config dns.listeningMode LOCAL

# 3. Reload the AP (frees :53), then start FTL's resolver on it.
sudo nmcli connection up dashboard-repeater-ap
sudo systemctl restart pihole-FTL

# 4. Verify: FTL (not dnsmasq) owns :53, and it resolves + blocks.
sudo ss -tulnp | grep ':53'            # expect pihole-FTL
nslookup doubleclick.net 127.0.0.1     # expect 0.0.0.0 (blocked)
nslookup google.com 127.0.0.1          # expect real IPs
```

This is reboot-proof: the drop-in (`port=0`) is read every time NetworkManager
respawns its dnsmasq, and FTL binds `0.0.0.0:53` cleanly once the conflict is
gone. Reconnect clients to the AP (toggle their Wi-Fi) so they pick up the new
DHCP-advertised DNS, then the Pi-hole query counter starts climbing.

**Captive portals.** With Pi-hole as the resolver, a *fixed* upstream (1.1.1.1)
gets blocked by hotel/airport portals before you log in — DNS dies and the login
page never appears. `install-agent.sh` installs a dispatcher
(`/etc/NetworkManager/dispatcher.d/50-pihole-upstream`) that repoints Pi-hole's
upstream at whatever DNS the **uplink** hands scout on every connect. Pre-login
the portal's DNS hijack then flows through Pi-hole (so the "Sign in" sheet pops
automatically); post-login Pi-hole still blocks ads (gravity is applied before
forwarding). Manual fallback if a portal is ever stubborn: from a device on the
AP, open `http://1.1.1.1` (a raw IP needs no DNS) to force the portal, log in,
then names resolve again.

> 🔒 **`LOCAL` vs hotel exposure:** `LOCAL` answers any directly-attached subnet
> (the AP, loopback — and the upstream/hotel subnet), never the public internet.
> That's a mild open-resolver exposure on the upstream side. To answer *only* the
> AP, set `dns.listeningMode SINGLE` + `dns.interface wlan0`, at the cost of FTL
> needing the AP interface up at start (less robust on reboot). `LOCAL` is the
> recommended balance for a travel router.
>
> Reminder: `pihole -g` (build the blocklist) and `pihole setpassword` both need
> `sudo`, and the dashboard's Pi-hole widget reads stats via `PIHOLE_PASSWORD`
> (see the Pi-hole widget section).

The agent runs `nmcli`, `iw`, and `iptables` via passwordless sudo.
`install-agent.sh` sets up the scoped sudoers drop-in automatically when `wlan0`
+ `nmcli` are present. For a node that's already installed (auto-update only
copies files), add it once:

```bash
echo "$USER ALL=(root) NOPASSWD: $(command -v nmcli), $(command -v iw), $(command -v iptables)" \
  | sudo tee /etc/sudoers.d/dashboard-nmcli && sudo chmod 0440 /etc/sudoers.d/dashboard-nmcli
sudo systemctl restart dashboard-agent
```

(`iptables` is needed for the Block/Unblock buttons below; `nmcli`/`iw` for
connect/repeater.)

**Block / unblock AP clients.** On the on-site (IP-served) dashboard, each entry
in the connected-devices list has a **Block**/**Unblock** button. Block adds a
firewall `DROP` for that client's MAC (`POST /wifi/block`) and deauthenticates it
— it stays off the internet even if it rejoins; Unblock removes the rule
(`POST /wifi/unblock`). Blocks persist in `dashboard/blocked-macs` and are
re-applied at agent startup (iptables rules don't survive a reboot). A
currently-blocked-but-disconnected device still shows in the list so you can
unblock it. The networked dashboard shows a `blocked` badge but no controls
(config is on-site only).

**Block unknown devices (curfew / allow-list).** The **Block unknown** toggle
above the device list flips the AP to allow-list mode: any client whose MAC
isn't in `KNOWN_DEVICES` is blocked and deauthenticated, and a background
enforcer (`POST /wifi/block-unknown`, every ~20s) keeps new unknowns out. Such
clients show a `curfew` tag instead of a button — to let one on, **name it** in
`KNOWN_DEVICES` (no longer "unknown"). Curfew blocks are dynamic (not written to
`blocked-macs`); the toggle state persists in `dashboard/block-unknown`. Manual
per-device blocks still apply independently and survive turning curfew off.

> ⚠️ **Security:** these endpoints are unauthenticated (like the rest of the
> dashboard) and reconfigure the node's networking. Intended for Tailscale-only
> access. Credentials are passed to `nmcli` as argv (no shell injection), but a
> PSK/password is briefly visible in the node's process list while connecting.

#### Troubleshooting the repeater

- **Client says "Incorrect password" but the password is right.** Usually a
  handshake/security mismatch, not the password. The AP is pinned to WPA2-PSK
  (RSN/CCMP, PMF off) for compatibility; if a client still fails, check the
  adapter actually supports AP mode (`iw list | grep -A8 "interface modes"`).
- **AP activates then drops; `journalctl -u NetworkManager` shows
  `dnsmasq … failed to bind … Address already in use` / `FAILED to start up`.**
  Repeater mode uses NetworkManager's `ipv4.method=shared`, which runs its own
  dnsmasq for DHCP/DNS on the AP interface. If another DHCP/DNS server already
  binds those ports it collides and the AP won't come up. Find the holder:
  ```bash
  sudo ss -ulnp | grep -E ':53|:67'
  ```
  - A **standalone `dnsmasq.service`** bound to `wlan1`/`:67` is the usual cause
    (e.g. a hand-rolled DHCP setup). If you don't need it, let NetworkManager own
    the AP: `sudo systemctl disable --now dnsmasq`, then re-Start the repeater.
  - **Pi-hole** (`pihole-FTL`) on `0.0.0.0:53` coexists fine — NM binds DNS to the
    AP gateway (`10.42.0.1:53`) specifically. If you do hit a `:53` clash, add a
    drop-in so NM does DHCP only and hands clients Pi-hole for DNS:
    ```
    # /etc/NetworkManager/dnsmasq-shared.d/00-repeater.conf
    port=0                      # no DNS in NM's shared dnsmasq → no :53 clash
    dhcp-option=6,<pihole-ip>   # AP clients use Pi-hole (keeps ad-blocking)
    ```
- **Verify it's up:** `nmcli -t -f NAME,DEVICE,STATE connection show --active`
  should list `dashboard-repeater-ap:wlan1:activated`, and `ss -ulnp` should show
  NM's dnsmasq on `10.42.0.1:53` + `:67`. Clients get `10.42.0.x` via NAT.

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
