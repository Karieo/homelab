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
pip3 install --user -r "${SCRIPT_DIR}/requirements.txt"

echo "==> Staging agent into ${DEST}"
mkdir -p "${DEST}"
cp "${SCRIPT_DIR}/agent.py" "${DEST}/agent.py"

echo "==> Installing systemd unit (rewriting User/paths for ${USER})"
sed -e "s|/home/clay|${HOME}|g" -e "s|User=clay|User=${USER}|" \
    "${SCRIPT_DIR}/systemd/dashboard-agent.service" \
    | sudo tee /etc/systemd/system/dashboard-agent.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable dashboard-agent
sudo systemctl restart dashboard-agent

if command -v ufw > /dev/null 2>&1; then
  echo "==> Opening firewall port 9090"
  sudo ufw allow 9090 || true
fi

echo "==> Done. Verify with:  curl http://localhost:9090/stats | python3 -m json.tool"
