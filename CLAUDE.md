## STATUS (updated 2026-07-10)
- Last did: implemented PLAN-meshtastic-panel — agent collector, dashboard
  widget, optional-service handling, tests — extended to run on **both**
  bastion and scout (plan only covered scout). See PR for details.
- Next up: install `meshtasticd` + the `meshtastic` pip package and physically
  wire up each node's radio (see README's new "Meshtastic widget" section);
  then work through the remaining ranked plans (ci-safe-deploys,
  secure-wifi-endpoints, background-sampler, alerter-coverage — in that
  order) if they haven't landed yet.
