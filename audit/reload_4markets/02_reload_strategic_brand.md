# 02 strategic_brand reload

- Loader: existing Phase 14 strategic_brand logic with 260518 workbook.
- Replace scope: ml_002, ml_006, ml_010, ml_012 only.
- Shape after: (4495, 16) (unchanged)
- Non-target 12 markets: exact row/cell equality PASS.
- Data changed brand rows: 19
- Data changed cells: 20

## Changed Brand Rows

| brand_id | ml_id | name | changed_columns | diff_cells | source_file_version |
| --- | --- | --- | --- | --- | --- |
| sb_002_00050 | ml_002 | 수프렙미니에스 | class | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_006_00060 | ml_006 | 에젯토 정 10/20mg | name, merge_name | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_010_00011 | ml_010 | 듀라스틴 | molecule | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00006 | ml_012 | 베노스틴 | strength_pack | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00007 | ml_012 | 훼렉스 | strength_pack | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00008 | ml_012 | 훼모럼 | strength_pack | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00009 | ml_012 | 페린젝트 | strength_pack | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00010 | ml_012 | 페린젝트 | strength_pack | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00011 | ml_012 | 페린젝트 | strength_pack | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00012 | ml_012 | 베노훼럼 | strength_pack | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00013 | ml_012 | 아네럼 | strength_pack | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00014 | ml_012 | 페로빈 | strength_pack | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00015 | ml_012 | 베노스틴 | strength_pack | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00016 | ml_012 | 훼렉스 | strength_pack | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00017 | ml_012 | 트리페릭 | strength_pack | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00018 | ml_012 | 모노퍼 | strength_pack | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00019 | ml_012 | 모노퍼 | strength_pack | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00020 | ml_012 | 훼로웰 | strength_pack | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00021 | ml_012 | 훼로웰 | strength_pack | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |

## Data Cell Diff

| brand_id | column | before | after |
| --- | --- | --- | --- |
| sb_002_00050 | class | Trisulfate | Sulfate 2종 |
| sb_006_00060 | name | 에젯토 정 10/10mg | 에젯토 정 10/20mg |
| sb_006_00060 | merge_name | 에젯토 정 10/10mg | 에젯토 정 10/20mg |
| sb_010_00011 | molecule | Tripegfilgrastim | PEGFILGRASTIM |
| sb_012_00006 | strength_pack | A.IV 540MG/ML 5ML | 100 |
| sb_012_00007 | strength_pack | A.IV 5400MG 10ML 5 | 200 |
| sb_012_00008 | strength_pack | A.IV 540MG/ML 5ML 5 | 100 |
| sb_012_00009 | strength_pack | V.IV 50MG/ML 10ML | 500 |
| sb_012_00010 | strength_pack | V.IV 50MG/ML 20ML | 1000 |
| sb_012_00011 | strength_pack | V.IV 50MG/ML 2ML | 100 |
| sb_012_00012 | strength_pack | A.IV 540MG/ML 5ML 5 | 100 |
| sb_012_00013 | strength_pack | A.IV 100MG 5ML 5 | 100 |
| sb_012_00014 | strength_pack | A.IV 540MG/ML 5ML | 100 |
| sb_012_00015 | strength_pack | A.IV 540MG/ML 10ML | 200 |
| sb_012_00016 | strength_pack | A.IV 2700MG 5ML 5 | 100 |
| sb_012_00017 | strength_pack | A.IV 6.75MG 4.5ML | 6.75 |
| sb_012_00018 | strength_pack | A.IV 417MG/ML 2ML | 200 |
| sb_012_00019 | strength_pack | A.IV 417MG/ML 5ML | 500 |
| sb_012_00020 | strength_pack | A.IV 100MG 5ML 5 | 100 |
| sb_012_00021 | strength_pack | A.IV 200MG 10ML 5 | 200 |
