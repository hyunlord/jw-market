# 01. Row Count + Period Recheck

Generated at: 2026-05-19T10:03:44+09:00
HEAD: `c65966f` / exact tag: `prototype-20-layer1-iqvia`

## UBIST Parquet

| Metric | Value |
|---|---:|
| total rows | 145,384,564 |
| unique 약품코드 | 20,461 |
| period min | 2021-01 |
| period max | 2026-04 |
| partitions | 64 |
| disk usage | 2.4G |

## IQVIA MariaDB Raw Tables

| source | row_count | unique_keys |
| ------ | --------- | ----------- |
| nsa    | 2,677,394 | 4           |
| csd    | 442,735   | 6           |
| chso   | 424,428   | 60          |

## Payload JSON Validity

| source | valid_payload_count | total     |
| ------ | ------------------- | --------- |
| nsa    | 2,677,394           | 2,677,394 |
| csd    | 442,735             | 442,735   |
| chso   | 424,428             | 424,428   |

Supporting CSV files: `ubist_period_distribution.csv`, `nsa_period_distribution.csv`, `csd_period_distribution.csv`, `chso_period_distribution.csv`.
