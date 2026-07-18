# Analysis-level block replay

`mart_analysis_level_block` rows are accepted only when both `build_version`
and `source_epoch` match the running API.

## Source epoch contract

The epoch hashes semantic policy plus explicit ETL data-version signals:

- mart metric `computation_version` and latest inserted row `computed_at`
- filter-dimension promotion marker per `(source, dimension_type)` slice
- catalog `source_file_version`, `ingested_at`, and `catalog_manifest_hash`

InnoDB physical metadata (`UPDATE_TIME`, estimated `TABLE_ROWS`, data length,
and index length) is intentionally excluded. Storage maintenance and
`ANALYZE TABLE` must not invalidate replay rows.

## Required rebuilds

Rebuild all analysis-level blocks after either event:

1. a dependent mart, filter-dimension slice, or catalog is promoted;
2. `CACHE_SOURCE_POLICY_VERSION` changes.

Run build and parity in different key orders so order-dependent state leaks
cannot pass validation:

```bash
PYTHONPATH=. MALB_MODE=build python3 pipeline/scripts/etl/build_analysis_level_blocks.py
PYTHONPATH=. MALB_MODE=parity MALB_PARITY_STRIDE=3137 \
  python3 pipeline/scripts/etl/build_analysis_level_blocks.py
```

The complete build must report exactly 3,138 keys. Before promotion, verify
that all rows match the runtime `source_epoch` and
`analysis-level-block-v4-filter-complete` build version.

General-view row filters are part of the replay identity. The bounded sidecar
contains only unfiltered market profiles, so filtered requests deliberately
miss and use mart-direct calculation unless that exact profile is baked. Never
reuse an unfiltered block for a request containing an analysis-level filter.

## Monitoring

The API emits `analysis_level_block_replay_stats` on the first occurrence of
each outcome and every 100 replay attempts. Alert when a deployed generation
has requests but its replay hit rate remains zero; that normally means the
runtime epoch and stored block epoch have diverged.
