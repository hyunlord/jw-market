# Phase 16-D Layer 2 Enriched Fact Summary

Status: PASS with matching warnings
Generated at: 2026-05-19T13:29:13+09:00

## Outputs

- Enriched parquet partitions: 16 (`parquet/enriched/ml_id=*/data.parquet`)
- Disk usage: `1.9G	parquet/enriched`
- Total enriched rows: 73,298,824
- Matched strategic_product IDs (sum by ml): 11,852 / 11,865
- Unmatched products: 13

## Source Totals

| source | rows | products |
| --- | --- | --- |
| chso | 179362 | 2917 |
| nsa | 74495 | 381 |
| ubist | 73044967 | 8934 |

## Match Confidence

| match_confidence | rows | products |
| --- | --- | --- |
| high | 73298824 | 12302 |

## ML Summary

| ml_id | rows | matched_products | total_products | product_match_rate | sources |
| --- | --- | --- | --- | --- | --- |
| ml_001 | 16218116 | 1411 | 1411 | 1.0 | {"ubist": 16218116} |
| ml_002 | 23723 | 186 | 186 | 1.0 | {"chso": 11160, "nsa": 12563} |
| ml_003 | 128340 | 2139 | 2139 | 1.0 | {"chso": 128340} |
| ml_004 | 520 | 8 | 10 | 0.8 | {"chso": 480, "nsa": 40} |
| ml_005 | 9097413 | 808 | 808 | 1.0 | {"ubist": 9097413} |
| ml_006 | 6466168 | 1127 | 1127 | 1.0 | {"ubist": 6466168} |
| ml_007 | 7465124 | 982 | 986 | 0.995943204868154 | {"ubist": 7465124} |
| ml_008 | 27601445 | 3547 | 3553 | 0.9983112862369828 | {"ubist": 27601445} |
| ml_009 | 6196701 | 1059 | 1060 | 0.9990566037735849 | {"ubist": 6196701} |
| ml_010 | 6066 | 20 | 20 | 1.0 | {"chso": 1200, "nsa": 4866} |
| ml_011 | 10194 | 60 | 60 | 1.0 | {"chso": 3600, "nsa": 6594} |
| ml_012 | 17133 | 76 | 76 | 1.0 | {"chso": 8902, "nsa": 8231} |
| ml_013 | 11469 | 42 | 42 | 1.0 | {"chso": 2460, "nsa": 9009} |
| ml_014 | 48433 | 331 | 331 | 1.0 | {"chso": 19860, "nsa": 28573} |
| ml_015 | 1406 | 4 | 4 | 1.0 | {"chso": 240, "nsa": 1166} |
| ml_016 | 6573 | 52 | 52 | 1.0 | {"chso": 3120, "nsa": 3453} |

## Matching Notes

- UBIST bridge: normalized `strategic_product.name` OR `merge_name` equals normalized UBIST `제품`.
- IQVIA NSA/CHSO bridge: `PRODUCT NAME KOR` + pack descriptor product title matching, with ATC gate where source ATC exists.
- CSD is intentionally skipped from product-level enriched_fact because CSD rows are call/rank supplemental facts, not product sales rows with a stable product brand key.
- `canonical_value` currently equals source sales amount (`rx_amt` / Values LC / VALUES LC SI PRICE), as requested for this Layer 2 pass.

## Warnings

- ml_004: 8/10 products (80.00%)

## Files

- `dry_run_ml_006.md`
- `loading_progress.txt`
- `enriched_summary.csv`
- `enriched_summary_by_source.csv`
- `match_quality.csv`
- `unmatched_products.csv`
- `channel_specialty_distribution.csv`
- `verification_queries.txt`
- `partition_files.txt`
- `disk_usage.txt`
