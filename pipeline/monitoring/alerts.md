# Alert Policy Reference

## Critical

- Pipeline job exits non-zero.
- `pipeline.enrich.match_rate < 0.80` for any `ml_id`.
- `pipeline.enrich.unmatched_products` increases by more than 5 from the Phase 16-D baseline of 13.
- `data.ubist.partitions_total` is below expected month count after a monthly load.
- MariaDB connection failures persist after retry attempts.

## Warning

- Job duration exceeds the previous four-run median by 2x.
- Output disk usage exceeds 80%.
- Any source file is skipped because it was already loaded when a full replace was expected.
- CSD or CHSO period coverage changes unexpectedly.

## Triage order

1. Check structured logs by `stage`, `source`, and `source_file`.
2. Confirm `/data` raw files for the current month exist.
3. Run `make verify` from the repo checkout or container shell.
4. Compare row counts to the latest viewer/current_state.html baseline.
