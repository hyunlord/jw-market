# Tier2 Mod28 Backfill Scripts

These scripts are the verified host-runner assets used for the Tier2 1-year
backfill after the idx22 false-positive gate failure was diagnosed.

## Files

- `run_tier2_mod28_chunk_retuned.sh`: creates one mod28 Tier2 backfill Job with
  sidecar injection disabled, `genos` node placement, `--days 365`,
  `--tier2-concurrent-sites 11`, `--delay-sec 5`, pre-seed, duplicate gate, and
  `--processed-by tier2_exact_rule_v1`.
- `post_tier2_chunk.sh`: captures final Job/Pod evidence, reads post-run DB
  counts, validates tier2 expiry/processor health inputs, and deletes the Job.
- `tier2_resume_after_idx22_tierspecific_loop.sh`: sequential resume loop for
  chunks 23-27. It uses tier-specific deltas so concurrent Tier1 appends do not
  trip the Tier2 gate.

## Gates

The loop aborts on runner failures, missing evidence, failed postcheck, nonzero
LLM calls, bad Tier2 expiry rows, non-`tier2_exact_rule_v1` processors,
`DeadlineExceeded`, unexpected Tier2 row deltas, or two consecutive zero-saved
chunks.

## Reuse Notes

The preserved scripts reflect the production run that completed the backfill.
For a new backfill, copy them to a host-owned work directory and adjust chunk
lists deliberately. Do not run chunks in parallel unless a separate load and
site-safety review approves it.
