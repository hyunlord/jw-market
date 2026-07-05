# wf196 v3 Rescore Scripts

These scripts are the Kubernetes Job based wf196 v3 rescore assets used after
long `kubectl exec` based retries hit exit 137 failures.

## Files

- `resume_loop.sh`: sequential chunk loop. It skips chunks marked `PASS`, stops
  on `ABORT`, and writes a `COMPLETE` marker only after all requested chunks
  pass.
- `run_rescore_chunk_job.sh`: creates one independent Kubernetes Job for a
  chunk, streams logs, waits for the pod, captures the final log, and requires
  the final verification summary before marking `PASS`.
- `rescore_chunk.py`: computes v3 scores for one chunk.
- `apply_chunk.py`: updates only eligible `workflow_196_optionB` /
  `llm_direct` / Tier1 rows that have backup coverage.
- `verify_chunk.py`: verifies updated rows against checkpoint output and checks
  backup integrity, cross-match immutability, and future-row exclusion.
- `catalog_text.txt`: prompt catalog text mounted with the chunk scripts.

## Gates

The runner requires `live_matches_checkpoint`, `backup_intact`,
`cross_match_unchanged`, and `future_unprocessed_unchanged` to all be true in the
final `VERIFY_SUMMARY`. It retries verification to absorb brief Galera stale
reads.

## Safety Boundaries

Do not rerun chunks already marked `PASS` without first proving idempotence for
the exact target set. Do not delete or alter the backup table. The scripts are
intended for event score rows only; they must not update `news_raw`, `events`,
Tier2 rows, or `cache_deep_analysis`.
