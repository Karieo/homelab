#!/usr/bin/env bash
#
# Install the dashboard static server as a systemd service.
# Run on bastion only.
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

echo "==> Done. Open http://bastion:9091"
