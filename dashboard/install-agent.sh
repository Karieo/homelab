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
# agent run nmcli via passwordless sudo so the dashboard can connect wlan0.
WIFI_IFACE="${WIFI_IFACE:-wlan0}"
NMCLI="$(command -v nmcli || true)"
if [ -e "/sys/class/net/${WIFI_IFACE}" ] && [ -n "$NMCLI" ]; then
  echo "==> ${WIFI_IFACE} present — enabling WiFi panel (sudoers for nmcli)"
  SUDOERS="/etc/sudoers.d/dashboard-nmcli"
  echo "${USER} ALL=(root) NOPASSWD: ${NMCLI}" | sudo tee "$SUDOERS" > /dev/null
  sudo chmod 0440 "$SUDOERS"
  sudo visudo -cf "$SUDOERS"   # validate; aborts on a malformed rule
fi

sudo systemctl daemon-reload
sudo systemctl enable dashboard-agent
sudo systemctl restart dashboard-agent

if command -v ufw > /dev/null 2>&1; then
  echo "==> Opening firewall port 9090"
  sudo ufw allow 9090 || true
fi

echo "==> Done. Verify with:  curl http://localhost:9090/stats | python3 -m json.tool"
