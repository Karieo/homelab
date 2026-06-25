#!/usr/bin/env bash
#
# Install the stats agent as a systemd service.
# Run on every node you want to monitor (bastion, scout, ...).
#
# Usage:  ./install-agent.sh
#
set -euo pipefail

DEST="${HOME}/dashboard"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing Python dependencies"
# Prefer distro packages — on Debian Bookworm / Raspberry Pi OS, PEP 668
# marks the environment "externally managed" and blocks plain `pip install`.
if command -v apt-get > /dev/null 2>&1; then
  sudo apt-get update -qq || true
  sudo apt-get install -y python3-flask python3-psutil
else
  # Fall back to pip, overriding PEP 668 if present.
  pip3 install --user -r "${SCRIPT_DIR}/requirements.txt" \
    || pip3 install --user --break-system-packages -r "${SCRIPT_DIR}/requirements.txt"
fi

echo "==> Staging agent into ${DEST}"
mkdir -p "${DEST}"
cp "${SCRIPT_DIR}/agent.py" "${DEST}/agent.py"

echo "==> Installing systemd unit (rewriting User/paths for ${USER})"
sed -e "s|/home/clay|${HOME}|g" -e "s|User=clay|User=${USER}|" \
    "${SCRIPT_DIR}/systemd/dashboard-agent.service" \
    | sudo tee /etc/systemd/system/dashboard-agent.service > /dev/null

# WiFi setup panel: if this node has a wireless interface and nmcli, let the
# agent run nmcli (and iw, for AP client counts) via passwordless sudo so the
# dashboard can connect wlan0 / run the repeater.
WIFI_IFACE="${WIFI_IFACE:-wlan0}"
NMCLI="$(command -v nmcli || true)"
if [ -e "/sys/class/net/${WIFI_IFACE}" ] && [ -n "$NMCLI" ]; then
  echo "==> ${WIFI_IFACE} present — enabling WiFi panel (sudoers for nmcli/iw/iptables)"
  IW="$(command -v iw || true)"
  IPT="$(command -v iptables || true)"
  RULE="${NMCLI}"
  [ -n "$IW" ] && RULE="${RULE}, ${IW}"
  # iptables: block/unblock AP clients (firewall DROP by MAC) from the dashboard.
  [ -n "$IPT" ] && RULE="${RULE}, ${IPT}"
  SUDOERS="/etc/sudoers.d/dashboard-nmcli"
  echo "${USER} ALL=(root) NOPASSWD: ${RULE}" | sudo tee "$SUDOERS" > /dev/null
  sudo chmod 0440 "$SUDOERS"
  sudo visudo -cf "$SUDOERS"   # validate; aborts on a malformed rule

  # NAT hook so repeater AP clients reach the internet even with ufw/Docker
  # managing the firewall (NetworkManager's shared NAT alone doesn't stick).
  if [ -d /etc/NetworkManager/dispatcher.d ]; then
    echo "==> Installing repeater NAT dispatcher hook"
    sudo cp "${SCRIPT_DIR}/dispatcher/90-dashboard-repeater-nat" \
      /etc/NetworkManager/dispatcher.d/90-dashboard-repeater-nat
    sudo chown root:root /etc/NetworkManager/dispatcher.d/90-dashboard-repeater-nat
    sudo chmod 0755 /etc/NetworkManager/dispatcher.d/90-dashboard-repeater-nat
  fi
fi

# Pi-hole travel-router hook: if this node runs Pi-hole, install the dispatcher
# that follows the uplink's DHCP DNS for Pi-hole's upstream — so captive portals
# still work with Pi-hole as the resolver (see README).
if command -v pihole-FTL > /dev/null 2>&1 && [ -d /etc/NetworkManager/dispatcher.d ]; then
  echo "==> Installing Pi-hole upstream-follows-uplink dispatcher hook"
  sudo cp "${SCRIPT_DIR}/dispatcher/50-pihole-upstream" \
    /etc/NetworkManager/dispatcher.d/50-pihole-upstream
  sudo chown root:root /etc/NetworkManager/dispatcher.d/50-pihole-upstream
  sudo chmod 0755 /etc/NetworkManager/dispatcher.d/50-pihole-upstream
fi

sudo systemctl daemon-reload
sudo systemctl enable dashboard-agent
sudo systemctl restart dashboard-agent

if command -v ufw > /dev/null 2>&1; then
  echo "==> Opening firewall port 9090"
  sudo ufw allow 9090 || true
fi

echo "==> Done. Verify with:  curl http://localhost:9090/stats | python3 -m json.tool"
