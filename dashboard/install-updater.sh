#!/usr/bin/env bash
#
# Install the auto-updater: a systemd timer that periodically pulls the
# repo and redeploys the dashboard/agent/alerter on this node.
#
# Run on any node you want to self-update (scout, bastion, ...). After
# this, you only need to merge to the tracked branch (main) — the node
# picks up changes within the timer interval (~15 min).
#
# Prerequisite: `git pull` must work non-interactively on this node
# (stored credential helper, a PAT, or an SSH remote). Test with:
#   cd ~/homelab && git pull
#
# Usage:  ./install-updater.sh
#
set -euo pipefail

DEST="${HOME}/dashboard"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMCTL="$(command -v systemctl)"

echo "==> Staging update.sh into ${DEST}"
mkdir -p "$DEST"
cp "${SCRIPT_DIR}/update.sh" "${DEST}/update.sh"
chmod +x "${DEST}/update.sh"

echo "==> Installing sudoers drop-in (passwordless restart of dashboard units)"
SUDOERS="/etc/sudoers.d/dashboard-update"
echo "${USER} ALL=(root) NOPASSWD: ${SYSTEMCTL} restart dashboard-agent.service, ${SYSTEMCTL} restart dashboard-alerter.service, ${SYSTEMCTL} restart dashboard.service" \
  | sudo tee "$SUDOERS" > /dev/null
sudo chmod 0440 "$SUDOERS"
sudo visudo -cf "$SUDOERS"   # validate; aborts the script if malformed

echo "==> Installing systemd service + timer (rewriting paths for ${USER})"
for unit in dashboard-update.service dashboard-update.timer; do
  sed -e "s|/home/clay|${HOME}|g" -e "s|User=clay|User=${USER}|" \
      "${SCRIPT_DIR}/systemd/${unit}" \
      | sudo tee "/etc/systemd/system/${unit}" > /dev/null
done

sudo systemctl daemon-reload
sudo systemctl enable --now dashboard-update.timer

echo "==> Done."
echo "    Next run:   systemctl list-timers dashboard-update.timer"
echo "    Logs:       journalctl -u dashboard-update.service -f"
echo "    Update now: ${DEST}/update.sh --force"
