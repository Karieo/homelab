#!/usr/bin/env bash
#
# Install the dashboard static server as a systemd service.
#
# Run on bastion (the main dashboard) and also on any node you want to
# manage on-site without a network — e.g. scout, so you can open
# http://<scout-AP-ip>:9091 from a phone on its access point and use the
# WiFi panel offline. When opened by raw IP the UI talks only to the local
# agent (see index.html), so it works with no internet/Tailscale/MagicDNS.
#
# Usage:  ./install-dashboard.sh
#
set -euo pipefail

DEST="${HOME}/dashboard"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Staging dashboard into ${DEST}"
mkdir -p "${DEST}"
cp "${SCRIPT_DIR}/index.html" "${DEST}/index.html"

echo "==> Installing systemd unit (rewriting User/paths for ${USER})"
sed -e "s|/home/clay|${HOME}|g" -e "s|User=clay|User=${USER}|" \
    "${SCRIPT_DIR}/systemd/dashboard.service" \
    | sudo tee /etc/systemd/system/dashboard.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable dashboard
sudo systemctl restart dashboard

if command -v ufw > /dev/null 2>&1; then
  echo "==> Opening firewall port 9091"
  sudo ufw allow 9091 || true
fi

echo "==> Done. Open http://bastion:9091 (or http://<this-node-ip>:9091 on-site)"
