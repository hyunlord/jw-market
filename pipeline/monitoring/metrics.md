# Monitoring Metrics

## ETL load metrics

| metric | type | labels | note |
|---|---|---|---|
| `pipeline.ubist.load.rows_inserted` | counter | `period`, `source_file` | rows written to parquet partitions |
| `pipeline.ubist.load.duration_seconds` | histogram | `stage` | total and per-file duration |
| `pipeline.ubist.load.errors` | counter | `source_file`, `error_type` | failed workbook/sheet count |
| `pipeline.iqvia.load.rows_inserted` | counter | `source` | NSA/CSD/CHSO rows committed |
| `pipeline.iqvia.load.errors` | counter | `source`, `source_file` | failed files/sheets |
| `pipeline.enrich.rows_written` | counter | `ml_id`, `source` | enriched rows by market/source |
| `pipeline.enrich.match_rate` | gauge | `ml_id` | matched strategic products / total |
| `pipeline.enrich.unmatched_products` | gauge | `ml_id` | unmatched strategic_product rows |

## Data quality metrics

| metric | type | expected |
|---|---|---|
| `data.ubist.partitions_total` | gauge | 64 after historical full load |
| `data.ubist.rows_total` | gauge | 145,384,564 after Phase 16-C-2 |
| `data.iqvia.rows_total` | gauge | 3,544,557 after Phase 16-C-3 |
| `data.enriched.rows_total` | gauge | 73,298,824 after Phase 16-D |
| `data.enriched.match_confidence_high` | gauge | near 100% |
