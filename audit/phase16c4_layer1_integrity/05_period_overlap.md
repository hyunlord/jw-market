# 05. Period Overlap

- UBIST parquet: 2021-01 ~ 2026-04 (64 months)
- IQVIA NSA: 2020Q3 ~ 2025Q4 (22 quarters; month-expanded for overlap)
- IQVIA CSD: 2024-12 ~ 2025-10 (6 loaded report months, 11 continuous calendar months)
- IQVIA CHSO: 2021-02 ~ 2026-01 (60 months)

## Overlap Summary

| source_pair                      | overlap_months_loaded | min_overlap | max_overlap |
| -------------------------------- | --------------------- | ----------- | ----------- |
| UBIST + NSA quarter-expanded     | 60                    | 2021-01     | 2025-12     |
| UBIST + CSD loaded report months | 6                     | 2024-12     | 2025-10     |
| UBIST + CHSO                     | 60                    | 2021-02     | 2026-01     |
| UBIST + CSD continuous range     | 11                    | 2024-12     | 2025-10     |

CSD is shortest and sparse: loaded report months are 2024-12, 2025-06, 2025-07, 2025-08, 2025-09, 2025-10; the continuous calendar span is 11 months.
