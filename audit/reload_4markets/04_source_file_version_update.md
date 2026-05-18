# 04 source_file_version update

- New source label: `MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx`
- `ml_market`: 4 target rows updated, all other ML rows unchanged.
- `cd_market`: 5 target rows updated, all other CD rows unchanged.
- No analyze_* / target_* changes in ml_market or cd_market: PASS.
- Note: existing script validators hard-code the old single source version, so this mixed-version update was verified by schema equality + explicit label-only diff instead.

## Update Summary

| table | updated_rows | target_ids | other_rows_unchanged |
| --- | --- | --- | --- |
| ml_market | 4 | ml_002, ml_006, ml_010, ml_012 | 12 |
| cd_market | 5 | cd_002, cd_006, cd_012, cd_013, cd_015 | 14 |
| strategic_brand | 1226 | ml_002, ml_006, ml_010, ml_012 | 3269 |
| strategic_product | 1409 | ml_002, ml_006, ml_010, ml_012 | 10456 |

## ml_market Label-only Diff

| ml_id | column | before | after |
| --- | --- | --- | --- |
| ml_002 | source_file_version | MI팀_시장분석 AI_시장 분석 Master Version (260422).xlsx | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| ml_002 | ingested_at | 2026-05-17T04:57:32.964373 | 2026-05-18T16:23:58 |
| ml_006 | source_file_version | MI팀_시장분석 AI_시장 분석 Master Version (260422).xlsx | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| ml_006 | ingested_at | 2026-05-17T04:57:32.964373 | 2026-05-18T16:23:58 |
| ml_010 | source_file_version | MI팀_시장분석 AI_시장 분석 Master Version (260422).xlsx | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| ml_010 | ingested_at | 2026-05-17T04:57:32.964373 | 2026-05-18T16:23:58 |
| ml_012 | source_file_version | MI팀_시장분석 AI_시장 분석 Master Version (260422).xlsx | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| ml_012 | ingested_at | 2026-05-17T04:57:32.964373 | 2026-05-18T16:23:58 |

## cd_market Label-only Diff

| cd_id | column | before | after |
| --- | --- | --- | --- |
| cd_002 | source_file_version | MI팀_시장분석 AI_시장 분석 Master Version (260422).xlsx | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| cd_002 | ingested_at | 2026-05-17T04:57:33.313742 | 2026-05-18T16:26:05 |
| cd_006 | source_file_version | MI팀_시장분석 AI_시장 분석 Master Version (260422).xlsx | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| cd_006 | ingested_at | 2026-05-17T04:57:33.313742 | 2026-05-18T16:26:05 |
| cd_012 | source_file_version | MI팀_시장분석 AI_시장 분석 Master Version (260422).xlsx | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| cd_012 | ingested_at | 2026-05-17T04:57:33.313742 | 2026-05-18T16:26:05 |
| cd_013 | source_file_version | MI팀_시장분석 AI_시장 분석 Master Version (260422).xlsx | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| cd_013 | ingested_at | 2026-05-17T04:57:33.313742 | 2026-05-18T16:26:05 |
| cd_015 | source_file_version | MI팀_시장분석 AI_시장 분석 Master Version (260422).xlsx | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| cd_015 | ingested_at | 2026-05-17T04:57:33.313742 | 2026-05-18T16:26:05 |
