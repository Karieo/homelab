## STATUS (updated 2026-07-10)
- Last did: implemented the remaining 4 ranked plans in order — ci-safe-deploys
  (GitHub Actions CI + deploy health check/rollback in update.sh),
  secure-wifi-endpoints (server-side trusted-source gate on POST /wifi/*),
  background-sampler (continuous history + cached /stats), alerter-coverage
  (disk-full + service-down alerts, persistent state). All 5 plans now done;
  26 tests in dashboard/tests.
- Next up: on the hardware — install `meshtasticd` + the `meshtastic` pip
  package and wire up each node's radio (README "Meshtastic widget"); set
  `TRUSTED_EXTRA_CIDRS` in the agent unit if WiFi config from the home LAN by
  raw IP is wanted; delete the PLAN-*.md files once their PRs are merged.
