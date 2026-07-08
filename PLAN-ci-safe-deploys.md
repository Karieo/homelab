# PLAN: CI + safe auto-deploys (health check & rollback)

**Rank: 1 of 5 — do this first.**

## Goal

Merging to `main` is literally the deploy mechanism: every node's
`dashboard-update.timer` pulls `main` every ~15 minutes, copies
`agent.py`/`index.html`/`alerter.py` into `~/dashboard`, and restarts the
services. Today there is **no CI and no post-deploy verification**, so a single
Python syntax error merged to `main` puts `dashboard-agent` into a
`Restart=always` crash loop on **every node** within 15 minutes, and nothing
tells you except silence.

This plan adds:

1. A GitHub Actions CI workflow (syntax check, endpoint smoke tests, shell
   script checks) that runs on every PR and push to `main`.
2. A post-restart **health check + file rollback** in `update.sh`, so a deploy
   that kills the agent restores the previous working files and sends a red
   notification instead of leaving the node dead.

Every other plan in this series lands through this pipe — that's why it goes
first.

## Files to touch

| File | Action |
|------|--------|
| `.github/workflows/ci.yml` | **create** |
| `dashboard/tests/__init__.py` | **create** (empty file) |
| `dashboard/tests/test_agent.py` | **create** |
| `dashboard/update.sh` | **modify** (backup + health check + rollback) |
| `README.md` | **modify** (document the health-check/rollback behavior in the Auto-update section) |

## Step-by-step

### Step 1 — smoke tests for the agent

Create `dashboard/tests/__init__.py` (empty).

Create `dashboard/tests/test_agent.py`:

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent  # noqa: E402


def client():
    agent.app.config["TESTING"] = True
    return agent.app.test_client()


def test_health():
    res = client().get("/health")
    assert res.status_code == 200
    assert res.get_json() == {"status": "ok"}


def test_stats_shape():
    res = client().get("/stats")
    assert res.status_code == 200
    data = res.get_json()
    for key in ("hostname", "role", "uptime", "cpu", "ram", "disk",
                "network", "containers", "timestamp"):
        assert key in data, f"missing {key}"
    assert isinstance(data["cpu"]["percent"], (int, float))
    assert isinstance(data["containers"], list)


def test_history_shape():
    res = client().get("/history?hours=24")
    assert res.status_code == 200
    data = res.get_json()
    for key in ("ts", "cpu", "temp", "ram", "disk"):
        assert isinstance(data[key], list)


def test_history_bad_hours_falls_back():
    res = client().get("/history?hours=banana")
    assert res.status_code == 200


def test_cors_headers():
    res = client().get("/health")
    assert res.headers["Access-Control-Allow-Origin"] == "*"


def test_wifi_routes_without_interface():
    # CI runners have no wlan0, so these must 400 cleanly (and must NOT
    # reach any sudo/nmcli code path).
    c = client()
    assert c.get("/wifi/status").status_code == 400
    assert c.post("/wifi/connect", json={"ssid": "x"}).status_code == 400
```

### Step 2 — CI workflow

Create `.github/workflows/ci.yml`:

```yaml
name: ci
on:
  push:
    branches: [main]
  pull_request:

jobs:
  python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install flask psutil pytest
      - name: Syntax check
        run: python -m py_compile dashboard/agent.py dashboard/alerter.py
      - name: Smoke tests
        run: pytest dashboard/tests -q

  shell:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: bash syntax
        run: |
          bash -n dashboard/update.sh
          bash -n dashboard/install-agent.sh
          bash -n dashboard/install-dashboard.sh
          bash -n dashboard/install-alerter.sh
          bash -n dashboard/install-updater.sh
          bash -n dashboard/dispatcher/50-pihole-upstream
          sh -n dashboard/dispatcher/90-dashboard-repeater-nat
      - name: shellcheck (errors only)
        run: |
          sudo apt-get install -y shellcheck
          shellcheck --severity=error --exclude=SC1090,SC1091 \
            dashboard/update.sh dashboard/install-*.sh \
            dashboard/dispatcher/50-pihole-upstream \
            dashboard/dispatcher/90-dashboard-repeater-nat
```

### Step 3 — health check + rollback in `update.sh`

All edits go **inside the `main()` function** (the whole body is parsed before
it runs, which is what makes self-overwrite safe — keep it that way).

3a. Immediately **before** the `echo "==> Staging files into ${DEST}"` line,
back up the currently-staged files:

```bash
  # Snapshot the currently-staged files so a broken deploy can be rolled back.
  local BACKUP="${DEST}/.rollback" f
  rm -rf "$BACKUP"
  mkdir -p "$BACKUP"
  for f in agent.py index.html alerter.py update.sh; do
    [ -f "${DEST}/${f}" ] && cp "${DEST}/${f}" "${BACKUP}/${f}"
  done
```

3b. **After** the service-restart `for` loop and **before** the final
`SUBJECT=` block, add the health check:

```bash
  # Post-deploy health check: if this node runs the agent, make sure it came
  # back up. On failure, restore the previous files and restart again.
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
```

3c. Update the Auto-update section of `README.md`: one short paragraph saying
each deploy is health-checked against `localhost:9090/health`; on failure the
previous files are restored and a red notification is sent, while the git repo
stays at the new commit (so the node doesn't retry-loop — push a fix, or run
`update.sh --force` to retry the same commit).

## Edge cases a weaker model would miss

- **`agent.py` has import-time side effects.** Importing it runs `init_db()`
  (creates `dashboard/stats.db` — already gitignored, fine),
  `reapply_blocklist()` (no-op when `dashboard/blocked-macs` doesn't exist),
  and conditionally starts the curfew thread (skipped — no `wlan1` in CI).
  Do **not** try to mock these away; they are safe as-is. But never call the
  `/wifi/block*` or `/wifi/repeater` endpoints with a fake interface present —
  they shell out to `sudo`.
- **`/stats` takes >1 second** (`cpu_percent(interval=1)`). Don't add a tight
  per-test timeout.
- **Rollback must NOT touch the git repo.** `update.sh` is fast-forward-only
  by design; rolling back the checkout would make the next timer run fail the
  ff-merge forever. Roll back only the *staged copies* in `~/dashboard` and
  leave the repo at the new commit — the next timer tick then sees
  "up to date" and does nothing, leaving the node healthy on old files until a
  fix is pushed.
- **The health-check loop must be inside `main()`** and must not use `exit`
  before `notify_deploy` — the notification is the only way the user learns a
  remote node rolled back.
- **`set -euo pipefail` is active.** Any bare command that can fail
  (e.g. `curl`, `systemctl restart` during rollback) needs `|| true` or an
  `if` guard, or the script dies mid-rollback. The `for i in 1 2 ...` loop
  avoids `seq`/`{1..12}` portability doubts.
- **Nodes that only run the static dashboard** (no `dashboard-agent.service`)
  must skip the health check entirely — hence the `list-unit-files` guard.
- **shellcheck at default severity fails on existing style warnings** in the
  install scripts. Pin `--severity=error` and exclude SC1090/SC1091 (the
  `notify.env` dynamic source), or CI is red on day one.
- **Don't `pip install -r dashboard/requirements.txt` plus pytest separately
  gated on file paths** — just install the three packages directly; the
  requirements file has no pins that matter for a smoke test.

## Acceptance criteria

1. `pytest dashboard/tests -q` passes locally (7 tests, 0 failures).
2. `python -m py_compile dashboard/agent.py dashboard/alerter.py` exits 0.
3. `bash -n dashboard/update.sh` exits 0 after the edits.
4. CI workflow runs and is green on the PR.
5. Negative test for CI: add a temporary commit with `def broken(:` in
   `agent.py` — the `python` job fails at the syntax-check step. Revert it.
6. Manual rollback test on a node (bastion or scout):
   `sed -i '1i raise SystemExit(1)' ~/dashboard/.rollback-test` is not needed —
   instead: commit a branch build where `agent.py`'s first line is
   `raise RuntimeError("deploy test")`, set `UPDATE_BRANCH=<that branch>` for
   one run of `~/dashboard/update.sh --force`, and verify:
   - the script waits ~60s, prints the rollback message,
   - `curl localhost:9090/health` succeeds afterwards (old agent restored),
   - a red deploy notification arrives (if notify.env is configured),
   - the script exits 1.
   Then reset `UPDATE_BRANCH` and run `update.sh --force` against `main`.
