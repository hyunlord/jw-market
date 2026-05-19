# Phase 16-C-3 IQVIA MariaDB Raw Load Summary

Status: PASS
Generated at: 2026-05-19T09:31:52+09:00

## Dry-run Schema Decisions
- NSA: wide quarterly source was normalized to one DB row per source row and quarter. Period columns were parsed from labels such as `3/2021_Values LC`; period range is 2020Q3 ~ 2025Q4.
- CHSO: wide monthly source was normalized to one DB row per source row and month. Period columns were parsed from labels such as `VALUES LC SI PRICE 2/2021`; period range is 2021-02 ~ 2026-01.
- CSD: report-style workbooks were stored as one DB row per non-empty source data row. `period_yyyymm` uses source file/report month; source row dates remain in the JSON payload. Period range is 2024-12 ~ 2025-10.

## Load Results
| Source | Files | File/sheets | Target table | Rows |
|---|---:|---:|---|---:|
| NSA | 3 | 3 | iqvia_nsa_quarterly_raw | 2,677,394 |
| CSD | 13 | 238 scanned / 225 non-empty loaded | iqvia_csd_monthly_raw | 442,735 |
| CHSO | 1 | 1 | iqvia_chso_monthly_raw | 424,428 |
| Total | 17 | 242 | 3 tables | 3,544,557 |

## Verification
- NSA distinct audit_code: 4
- Payload JSON validity: all loaded rows pass `JSON_VALID(payload)`.
- Batch insert completed with source-level error isolation; loader reported errors=0 for NSA, CSD, and CHSO.
- CSD was reloaded after correcting period semantics to source file/report month.
- CSD source workbooks contain 238 sheets; 225 sheet keys produced non-empty raw rows in MariaDB.

## Output Files
- dry_run_nsa.md
- dry_run_csd.md
- dry_run_chso.md
- loading_progress.txt
- nsa_period_distribution.csv
- csd_period_distribution.csv
- chso_period_distribution.csv
- nsa_top_audit_codes.csv
- csd_channel_distribution.csv
- payload_validity_check.txt
