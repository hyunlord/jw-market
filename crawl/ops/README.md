# Crawl Operations Assets

This directory stores operational scripts that were previously only present as
host `/tmp` files during the Tier1/Tier2 crawl rollout and wf196 v3 rescore
work.

These scripts are preserved for reproducibility and incident recovery. They are
not automatically invoked by importing this repository. Read each subdirectory
README before running anything, and keep production gates enabled.

## Directories

- `tier2_backfill/`: mod28 Tier2 backfill runner and tier-specific postcheck.
- `wf196_v3_rescore/`: wf196 v3 chunk rescore Job runner and chunk scripts.

## Operational Lessons

- Long-running production work should run as independent Kubernetes Jobs, not as
  long `kubectl exec` sessions into live pods.
- A chunk is successful only after its final verification gate passes. Pod phase
  alone is insufficient.
- Concurrent production writers require target-specific gates. Tier2 backfill
  checks must count only `tier=2` / `tier2_exact_rule_v1` rows so Tier1 appends
  do not create false mismatches.
- Galera reads can be briefly stale after writes. Verification should retry
  before declaring failure.
- State, logs, and evidence directories are runtime artifacts. Do not commit
  them.
