#!/usr/bin/env bash
#
# Pull the latest dashboard from git and redeploy it locally.
#
# Designed to run unattended on a timer (see install-updater.sh): it only
# acts when the tracked branch has new commits, and restarts just the
# services that are installed on this node. Fast-forward only — it will
# never clobber local changes, so keep your per-node config committed to
# the repo (secrets stay in the systemd unit env / notify.env, not in files).
#
# The repo location is independent of where this script is installed
# (it's staged into ~/dashboard, which is *not* inside the repo). Override
# with DASHBOARD_REPO if your clone isn't at ~/homelab.
#
# Usage:  ./update.sh [--force]
#
set -euo pipefail

REPO_DIR="${DASHBOARD_REPO:-$HOME/homelab}"
SRC="${REPO_DIR}/dashboard"
DEST="${HOME}/dashboard"
BRANCH="${UPDATE_BRANCH:-main}"

# Optional notification channel — set DISCORD_WEBHOOK_URL / NTFY_URL here
# (shared with the alerter), so deploys announce themselves. Sourced for
# manual runs; the systemd unit also reads it via EnvironmentFile.
NOTIFY_ENV="${DEST}/notify.env"
if [ -f "$NOTIFY_ENV" ]; then
  set -a; . "$NOTIFY_ENV"; set +a
fi

# notify_deploy <ok|fail> <message>
notify_deploy() {
  local status="$1" message="$2" host color
  host="$(hostname)"
  [ "$status" = "fail" ] && color=15158332 || color=3066993
  if [ -n "${DISCORD_WEBHOOK_URL:-}" ]; then
    local payload
    payload="$(python3 -c 'import json,sys; print(json.dumps({"embeds":[{"title":sys.argv[1]+" deploy","description":sys.argv[2],"color":int(sys.argv[3])}]}))' \
      "$host" "$message" "$color")"
    curl -fsS -m 10 -A "homelab-ops-updater/1.0" \
      -H "Content-Type: application/json" -d "$payload" \
      "$DISCORD_WEBHOOK_URL" >/dev/null 2>&1 || echo "discord notify failed" >&2
  fi
  if [ -n "${NTFY_URL:-}" ]; then
    curl -fsS -m 10 -H "Title: ${host} deploy" -d "$message" \
      "$NTFY_URL" >/dev/null 2>&1 || true
  fi
}

# Wrapped in a function so the whole body is parsed before it runs — safe
# even when this script overwrites its own staged copy below.
main() {
  local FORCE="${1:-}"

  if [ ! -d "${REPO_DIR}/.git" ]; then
    echo "ERROR: no git repo at ${REPO_DIR} (set DASHBOARD_REPO)" >&2
    exit 1
  fi

  cd "$REPO_DIR"
  git fetch --quiet origin "$BRANCH"
  local LOCAL REMOTE
  LOCAL="$(git rev-parse @)"
  REMOTE="$(git rev-parse "origin/${BRANCH}")"

  if [ "$LOCAL" = "$REMOTE" ] && [ "$FORCE" != "--force" ]; then
    echo "$(date -Is) up to date (${LOCAL:0:8})"
    exit 0
  fi

  echo "$(date -Is) updating ${LOCAL:0:8} -> ${REMOTE:0:8}"
  if ! git merge --ff-only --quiet "origin/${BRANCH}"; then
    echo "ERROR: cannot fast-forward — local changes on this node?" >&2
    echo "Commit them to the repo (or 'git stash') and re-run." >&2
    notify_deploy fail "update failed: cannot fast-forward (local changes on $(hostname)?)"
    exit 1
  fi

  # Snapshot the currently-staged files so a broken deploy can be rolled back.
  local BACKUP="${DEST}/.rollback" f
  rm -rf "$BACKUP"
  mkdir -p "$BACKUP"
  for f in agent.py index.html alerter.py update.sh; do
    [ -f "${DEST}/${f}" ] && cp "${DEST}/${f}" "${BACKUP}/${f}"
  done

  echo "==> Staging files into ${DEST}"
  mkdir -p "$DEST"
  cp "${SRC}/agent.py" "${DEST}/agent.py"
  [ -f "${SRC}/index.html" ] && cp "${SRC}/index.html" "${DEST}/index.html"
  [ -f "${SRC}/alerter.py" ] && cp "${SRC}/alerter.py" "${DEST}/alerter.py"
  # Self-heal: keep the staged updater current too.
  [ -f "${SRC}/update.sh" ] && cp "${SRC}/update.sh" "${DEST}/update.sh" \
    && chmod +x "${DEST}/update.sh"

  # Restart only the services that are actually installed on this node.
  # (The static dashboard server re-reads index.html per request, but a
  # restart is harmless and keeps things simple.)
  local svc
  for svc in dashboard-agent dashboard-alerter dashboard; do
    if systemctl list-unit-files "${svc}.service" --no-legend 2>/dev/null \
         | grep -q "${svc}.service"; then
      sudo systemctl restart "${svc}.service" && echo "restarted ${svc}"
    fi
  done

  # Post-deploy health check: if this node runs the agent, make sure it came
  # back up. On failure, restore the previous files and restart again. The
  # repo stays at the new commit (fast-forward only, never rolled back), so
  # the next timer tick sees "up to date" and leaves the node healthy on the
  # old files until a fix is pushed.
  if systemctl list-unit-files dashboard-agent.service --no-legend 2>/dev/null \
       | grep -q dashboard-agent.service; then
    local healthy=0 i
    for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
      if curl -fsS -m 3 http://localhost:9090/health >/dev/null 2>&1; then
        healthy=1
        break
      fi
      sleep 5
    done
    if [ "$healthy" != "1" ]; then
      echo "ERROR: agent failed health check after deploy — rolling back files" >&2
      local restored=0
      for f in agent.py index.html alerter.py update.sh; do
        if [ -f "${BACKUP}/${f}" ]; then
          cp "${BACKUP}/${f}" "${DEST}/${f}"
          restored=1
        fi
      done
      [ -f "${DEST}/update.sh" ] && chmod +x "${DEST}/update.sh"
      if [ "$restored" = "1" ]; then
        for svc in dashboard-agent dashboard-alerter dashboard; do
          if systemctl list-unit-files "${svc}.service" --no-legend 2>/dev/null \
               | grep -q "${svc}.service"; then
            sudo systemctl restart "${svc}.service" || true
          fi
        done
        notify_deploy fail "deploy ${REMOTE:0:8} FAILED health check — rolled back staged files to ${LOCAL:0:8} (repo left at ${REMOTE:0:8})"
      else
        notify_deploy fail "deploy ${REMOTE:0:8} FAILED health check — nothing to roll back (first install?)"
      fi
      exit 1
    fi
  fi

  local SUBJECT
  SUBJECT="$(git log -1 --pretty=%s "$REMOTE" 2>/dev/null || echo "")"
  echo "$(date -Is) update complete (${REMOTE:0:8})"
  notify_deploy ok "updated ${LOCAL:0:8} → ${REMOTE:0:8} — ${SUBJECT}"
}

main "$@"
