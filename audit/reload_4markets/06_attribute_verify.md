# 06 attribute verify

- 19 strategic_brand data rows changed exactly in the approved scope.
- 19 strategic_product data rows changed exactly in the approved scope.
- 에젯토 SKU 3종 확인: 10/10mg, 10/20mg, 10/40mg 각 1행.
- 페린젝트/베노훼럼 strength_pack 16개 numeric 반영.

## Verification Checks

| check | expected | actual | status |
| --- | --- | --- | --- |
| ml_002 수프렙미니에스 class | Sulfate 2종 | Sulfate 2종 | PASS |
| ml_006 에젯토 SKU 3종 | {"에젯토 정 10/10mg": 1, "에젯토 정 10/20mg": 1, "에젯토 정 10/40mg": 1} | {"에젯토 정 10/10mg": 1, "에젯토 정 10/20mg": 1, "에젯토 정 10/40mg": 1} | PASS |
| ml_010 듀라스틴 molecule | PEGFILGRASTIM | PEGFILGRASTIM | PASS |
| ml_012 sb_012_00006 strength_pack | 100 | 100 | PASS |
| ml_012 sb_012_00007 strength_pack | 200 | 200 | PASS |
| ml_012 sb_012_00008 strength_pack | 100 | 100 | PASS |
| ml_012 sb_012_00009 strength_pack | 500 | 500 | PASS |
| ml_012 sb_012_00010 strength_pack | 1000 | 1000 | PASS |
| ml_012 sb_012_00011 strength_pack | 100 | 100 | PASS |
| ml_012 sb_012_00012 strength_pack | 100 | 100 | PASS |
| ml_012 sb_012_00013 strength_pack | 100 | 100 | PASS |
| ml_012 sb_012_00014 strength_pack | 100 | 100 | PASS |
| ml_012 sb_012_00015 strength_pack | 200 | 200 | PASS |
| ml_012 sb_012_00016 strength_pack | 100 | 100 | PASS |
| ml_012 sb_012_00017 strength_pack | 6.75 | 6.75 | PASS |
| ml_012 sb_012_00018 strength_pack | 200 | 200 | PASS |
| ml_012 sb_012_00019 strength_pack | 500 | 500 | PASS |
| ml_012 sb_012_00020 strength_pack | 100 | 100 | PASS |
| ml_012 sb_012_00021 strength_pack | 200 | 200 | PASS |

## ml_006 에젯토 After

| brand_id | name | strength_pack | class | molecule | 제조사 | source_file_version |
| --- | --- | --- | --- | --- | --- | --- |
| sb_006_00059 | 에젯토 정 10/10mg | 10/10mg | ATV/EZE | atorvastatin calcium trihydrate  ( as atorvastatin),  ezetimibe | 명문제약 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_006_00060 | 에젯토 정 10/20mg | 20/10mg | ATV/EZE | atorvastatin calcium trihydrate  ( as atorvastatin),  ezetimibe | 명문제약 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_006_00061 | 에젯토 정 10/40mg | 40/10mg | ATV/EZE | atorvastatin calcium trihydrate  ( as atorvastatin),  ezetimibe | 명문제약 | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |

## ml_012 Strength After

| brand_id | name | strength_pack | molecule | dosage_form | source_file_version |
| --- | --- | --- | --- | --- | --- |
| sb_012_00006 | 베노스틴 | 100 | IRON FERRIC | IV Iron | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00007 | 훼렉스 | 200 | IRON FERRIC | IV Iron | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00008 | 훼모럼 | 100 | IRON FERRIC | IV Iron | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00009 | 페린젝트 | 500 | IRON FERRIC | IV Iron | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00010 | 페린젝트 | 1000 | IRON FERRIC | IV Iron | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00011 | 페린젝트 | 100 | IRON FERRIC | IV Iron | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00012 | 베노훼럼 | 100 | IRON FERRIC | IV Iron | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00013 | 아네럼 | 100 | IRON FERRIC | IV Iron | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00014 | 페로빈 | 100 | IRON FERRIC | IV Iron | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00015 | 베노스틴 | 200 | IRON FERRIC | IV Iron | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00016 | 훼렉스 | 100 | IRON FERRIC | IV Iron | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00017 | 트리페릭 | 6.75 | IRON FERRIC | IV Iron | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00018 | 모노퍼 | 200 | IRON FERRIC | IV Iron | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00019 | 모노퍼 | 500 | IRON FERRIC | IV Iron | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00020 | 훼로웰 | 100 | IRON FERRIC | IV Iron | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
| sb_012_00021 | 훼로웰 | 200 | IRON FERRIC | IV Iron | MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx |
