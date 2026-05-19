# Phase 16-C-4 Layer 1 Integrity Audit Summary

Status: PASS (read-only audit)
Generated at: 2026-05-19T10:03:44+09:00

## Layer 1 Final State

| Source | Storage | Rows | Period |
|---|---|---:|---|
| UBIST | parquet hive partition | 145,384,564 | 2021-01 ~ 2026-04 |
| IQVIA NSA | MariaDB `iqvia_nsa_quarterly_raw` | 2,677,394 | 2020Q3 ~ 2025Q4 |
| IQVIA CSD | MariaDB `iqvia_csd_monthly_raw` | 442,735 | 2024-12 ~ 2025-10 |
| IQVIA CHSO | MariaDB `iqvia_chso_monthly_raw` | 424,428 | 2021-02 ~ 2026-01 |
| Total | hybrid Layer 1 | 148,929,121 | - |

## Catalog Matching Findings

- Direct UBIST key match is not currently possible: `strategic_product` has no `약품코드` column.
- Direct IQVIA NSA audit-code match is not currently possible: `strategic_product` has no `AUDIT_CODE`, and NSA `audit_code` values are channel/customer codes.
- Candidate UBIST bracket-code bridge: 7,887 / 7,887 strategic products with source bracket codes matched UBIST `성분용량` bracket codes.
- Catalog ATC coverage estimate: UBIST rows across catalog ATC codes = 13,297,738; NSA rows across catalog ATC codes = 342,817; NSA target-channel rows = 49,562.
- Full 16-market coverage table is in `06_catalog_coverage.csv`.

## Period Overlap

| source_pair                      | overlap_months_loaded | min_overlap | max_overlap |
| -------------------------------- | --------------------- | ----------- | ----------- |
| UBIST + NSA quarter-expanded     | 60                    | 2021-01     | 2025-12     |
| UBIST + CSD loaded report months | 6                     | 2024-12     | 2025-10     |
| UBIST + CHSO                     | 60                    | 2021-02     | 2026-01     |
| UBIST + CSD continuous range     | 11                    | 2024-12     | 2025-10     |

## Phase 16-D Notes

1. Do not assume direct UBIST drug code or IQVIA product audit code exists in `strategic_product`.
2. UBIST: use a formal product-code bridge or a validated candidate using source bracket code/product metadata. ATC coverage is market filtering, not product identity.
3. IQVIA NSA: map `audit_code` as channel/customer (`KCPA_DIRECT` canonicalizes to `KCPA`), then match products via payload fields (`PRODUCT NAME KOR`, `PACK DESC`, `MFR NAME KOR`, `ATC 4 CODE`, `MOLECULE DESC`).
4. CSD is a short/sparse source: 6 loaded report months inside an 11-month calendar span.

## Output Files

- `01_row_period_recheck.md`
- `02_strategic_brand_columns.md`
- `03_ubist_drug_match.md`
- `04_iqvia_nsa_audit_match.md`
- `05_period_overlap.md`
- `06_catalog_coverage.csv`
- supporting CSVs for period, ATC/channel, and candidate key checks
