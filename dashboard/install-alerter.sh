#!/usr/bin/env bash
#
# Install the alerter as a systemd service.
# Run on bastion (it polls all nodes and sends notifications).
#
# Set your ntfy channel afterwards with:
#   sudo systemctl edit --full dashboard-alerter   # uncomment Environment=NTFY_URL=...
#   sudo systemctl restart dashboard-alerter
#
# Usage:  ./install-alerter.sh
#
set -euo pipefail

DEST="${HOME}/dashboard"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Staging alerter into ${DEST}"
mkdir -p "${DEST}"
cp "${SCRIPT_DIR}/alerter.py" "${DEST}/alerter.py"

echo "==> Installing systemd unit (rewriting User/paths for ${USER})"
sed -e "s|/home/clay|${HOME}|g" -e "s|User=clay|User=${USER}|" \
    "${SCRIPT_DIR}/systemd/dashboard-alerter.service" \
    | sudo tee /etc/systemd/system/dashboard-alerter.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable dashboard-alerter
sudo systemctl restart dashboard-alerter

echo "==> Done. It logs to the journal (no ntfy channel yet):"
echo "    journalctl -u dashboard-alerter -f"
echo "    Set NTFY_URL via: sudo systemctl edit --full dashboard-alerter"
