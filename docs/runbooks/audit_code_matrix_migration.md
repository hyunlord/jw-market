# IQVIA audit_code_matrix migration runbook

This runbook preserves the reproducible path for adding IQVIA
`audit_code_matrix` to an existing D-2 mart without rebuilding or replacing
`mart_general_brand_metric` rows.

## Scope

- Target table: `mart_general_brand_metric`
- Target rows: `source='iqvia_nsa'`
- Mutated column: `audit_code_matrix` only
- Protected columns checked before and after:
  `metric_history`, `raw_value_history`, `dimension_data`, `by_dimension`
- Galera batch cap: 200 rows

Do not use this script for UBIST rows or operating schemas unless the operator
explicitly passes the protected-target override after a separate gate.

## Dry-run

Build the canonical update plan from the Layer 3 general mart builder and write
validation JSONL under `/tmp`:

```bash
python -m pipeline.scripts.deploy.audit_code_matrix_migration \
  --env-file pipeline/docker/.env \
  --target-db jw_mart_d2 \
  --dry-run \
  --output-dir /tmp/audit_code_matrix_migration
```

Expected evidence:

- `planned_updates` equals the IQVIA brand-row count for the selected scope.
- `nonempty_matrices` is nonzero.
- No target DB write is performed.

For isolated smoke checks, add `--limit-atc4 3` or `--max-rows 10000`. Do not
use those flags for a full D-2 apply.

## Apply

```bash
python -m pipeline.scripts.deploy.audit_code_matrix_migration \
  --env-file pipeline/docker/.env \
  --target-db jw_mart_d2 \
  --apply \
  --batch-size 200 \
  --output-dir /tmp/audit_code_matrix_migration
```

The script:

1. Adds `audit_code_matrix` if the column is absent.
2. Builds canonical IQVIA general rows with `compute_general(..., dry_run=True)`.
3. Updates `audit_code_matrix` by `(source, brand_key, atc4_code, measure)`.
4. Verifies protected-column fingerprint before and after.
5. Verifies `JSON_VALID(audit_code_matrix)` for all non-null IQVIA matrices.

Hard-stop if any protected fingerprint changes, if invalid JSON is reported, or
if row counts differ from the pre-state evidence.

## Raw-value spot checks

Use the audit evidence query set to compare representative matrix values against
the raw NSA source. At minimum include:

- C10A1 / 리바로 / KPA / latest quarter
- C10A1 / 리바로 / KHPA / latest quarter
- C10A1 / 리바로 / KCPA / latest quarter

The matrix value must equal raw `iqvia_nsa_quarterly_raw` SUM for the same
brand, ATC4, audit code, measure, and period.

## Rollback

Rollback clears only IQVIA matrices and leaves all other columns untouched:

```bash
python -m pipeline.scripts.deploy.audit_code_matrix_migration \
  --env-file pipeline/docker/.env \
  --target-db jw_mart_d2 \
  --rollback-null \
  --batch-size 200
```

After rollback, rerun the protected fingerprint check and confirm
`audit_code_matrix IS NULL` for all `source='iqvia_nsa'` rows.

## Full mart regeneration path

The normal Layer 3 builder also generates the column:

```bash
python -m pipeline.etl.io.mart.layer3_compute_general_v3 \
  --source iqvia_nsa \
  --dry-run \
  --output-dir /tmp/general_iqvia_validate
```

Full insert mode is a separate mart regeneration path. This runbook is for the
safer in-place D-2 backfill when row identity and protected columns must remain
stable.
