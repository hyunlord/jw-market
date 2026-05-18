# 03 strategic_product reload

- Loader: existing Phase 14 strategic_product logic with 260518 source contexts.
- Replace scope: products whose `ml_id` is in ml_002, ml_006, ml_010, ml_012 only.
- Shape after: (11865, 17) (unchanged)
- Non-target 12 markets: exact row/cell equality PASS.
- Data changed product rows: 19
- Data changed cells: 39

## Changed Product Rows

| product_id | brand_id | ml_id | name | changed_columns | diff_cells | source_file_version |
| --- | --- | --- | --- | --- | --- | --- |
| sp_002_00050_001 | sb_002_00050 | ml_002 | SUPREP MINI S C.T.F 25T*1P 300 | class | 1 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_006_00060_001 | sb_006_00060 | ml_006 | 에젯토 정 10/20mg 20/10mg | name, merge_name | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_010_00011_001 | sb_010_00011 | ml_010 | DULASTIN PRE-F SRN SC 6MG 0.6ML | name, molecule, dosage_form, strength_pack | 4 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_012_00006_001 | sb_012_00006 | ml_012 | 베노스틴 100 | name, strength_pack | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_012_00007_001 | sb_012_00007 | ml_012 | 훼렉스 200 | name, strength_pack | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_012_00008_001 | sb_012_00008 | ml_012 | 훼모럼 100 | name, strength_pack | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_012_00009_001 | sb_012_00009 | ml_012 | 페린젝트 500 | name, strength_pack | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_012_00010_001 | sb_012_00010 | ml_012 | 페린젝트 1000 | name, strength_pack | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_012_00011_001 | sb_012_00011 | ml_012 | 페린젝트 100 | name, strength_pack | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_012_00012_001 | sb_012_00012 | ml_012 | 베노훼럼 100 | name, strength_pack | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_012_00013_001 | sb_012_00013 | ml_012 | 아네럼 100 | name, strength_pack | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_012_00014_001 | sb_012_00014 | ml_012 | 페로빈 100 | name, strength_pack | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_012_00015_001 | sb_012_00015 | ml_012 | 베노스틴 200 | name, strength_pack | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_012_00016_001 | sb_012_00016 | ml_012 | 훼렉스 100 | name, strength_pack | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_012_00017_001 | sb_012_00017 | ml_012 | 트리페릭 6.75 | name, strength_pack | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_012_00018_001 | sb_012_00018 | ml_012 | 모노퍼 200 | name, strength_pack | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_012_00019_001 | sb_012_00019 | ml_012 | 모노퍼 500 | name, strength_pack | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_012_00020_001 | sb_012_00020 | ml_012 | 훼로웰 100 | name, strength_pack | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sp_012_00021_001 | sb_012_00021 | ml_012 | 훼로웰 200 | name, strength_pack | 2 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |

## Data Cell Diff

| product_id | column | before | after |
| --- | --- | --- | --- |
| sp_002_00050_001 | class | Trisulfate | Sulfate 2종 |
| sp_006_00060_001 | name | 에젯토 정 10/10mg 20/10mg | 에젯토 정 10/20mg 20/10mg |
| sp_006_00060_001 | merge_name | 에젯토 정 10/10mg | 에젯토 정 10/20mg |
| sp_010_00011_001 | name | 듀라스틴 | DULASTIN PRE-F SRN SC 6MG 0.6ML |
| sp_010_00011_001 | molecule | Tripegfilgrastim | PEGFILGRASTIM |
| sp_010_00011_001 | dosage_form |  | Parental Retard S C Pre-Filled Syringes |
| sp_010_00011_001 | strength_pack |  | PRE-F SRN SC 6MG 0.6ML |
| sp_012_00006_001 | name | 베노스틴 A.IV 540MG/ML 5ML | 베노스틴 100 |
| sp_012_00006_001 | strength_pack | A.IV 540MG/ML 5ML | 100 |
| sp_012_00007_001 | name | 훼렉스 A.IV 5400MG 10ML 5 | 훼렉스 200 |
| sp_012_00007_001 | strength_pack | A.IV 5400MG 10ML 5 | 200 |
| sp_012_00008_001 | name | 훼모럼 A.IV 540MG/ML 5ML 5 | 훼모럼 100 |
| sp_012_00008_001 | strength_pack | A.IV 540MG/ML 5ML 5 | 100 |
| sp_012_00009_001 | name | 페린젝트 V.IV 50MG/ML 10ML | 페린젝트 500 |
| sp_012_00009_001 | strength_pack | V.IV 50MG/ML 10ML | 500 |
| sp_012_00010_001 | name | 페린젝트 V.IV 50MG/ML 20ML | 페린젝트 1000 |
| sp_012_00010_001 | strength_pack | V.IV 50MG/ML 20ML | 1000 |
| sp_012_00011_001 | name | 페린젝트 V.IV 50MG/ML 2ML | 페린젝트 100 |
| sp_012_00011_001 | strength_pack | V.IV 50MG/ML 2ML | 100 |
| sp_012_00012_001 | name | 베노훼럼 A.IV 540MG/ML 5ML 5 | 베노훼럼 100 |
| sp_012_00012_001 | strength_pack | A.IV 540MG/ML 5ML 5 | 100 |
| sp_012_00013_001 | name | 아네럼 A.IV 100MG 5ML 5 | 아네럼 100 |
| sp_012_00013_001 | strength_pack | A.IV 100MG 5ML 5 | 100 |
| sp_012_00014_001 | name | 페로빈 A.IV 540MG/ML 5ML | 페로빈 100 |
| sp_012_00014_001 | strength_pack | A.IV 540MG/ML 5ML | 100 |
| sp_012_00015_001 | name | 베노스틴 A.IV 540MG/ML 10ML | 베노스틴 200 |
| sp_012_00015_001 | strength_pack | A.IV 540MG/ML 10ML | 200 |
| sp_012_00016_001 | name | 훼렉스 A.IV 2700MG 5ML 5 | 훼렉스 100 |
| sp_012_00016_001 | strength_pack | A.IV 2700MG 5ML 5 | 100 |
| sp_012_00017_001 | name | 트리페릭 A.IV 6.75MG 4.5ML | 트리페릭 6.75 |
| sp_012_00017_001 | strength_pack | A.IV 6.75MG 4.5ML | 6.75 |
| sp_012_00018_001 | name | 모노퍼 A.IV 417MG/ML 2ML | 모노퍼 200 |
| sp_012_00018_001 | strength_pack | A.IV 417MG/ML 2ML | 200 |
| sp_012_00019_001 | name | 모노퍼 A.IV 417MG/ML 5ML | 모노퍼 500 |
| sp_012_00019_001 | strength_pack | A.IV 417MG/ML 5ML | 500 |
| sp_012_00020_001 | name | 훼로웰 A.IV 100MG 5ML 5 | 훼로웰 100 |
| sp_012_00020_001 | strength_pack | A.IV 100MG 5ML 5 | 100 |
| sp_012_00021_001 | name | 훼로웰 A.IV 200MG 10ML 5 | 훼로웰 200 |
| sp_012_00021_001 | strength_pack | A.IV 200MG 10ML 5 | 200 |
