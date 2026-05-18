# 08 summary

## Overall Status

| gate | result | status |
| --- | --- | --- |
| strategic_brand shape unchanged | (4495, 16) | PASS |
| strategic_product shape unchanged | (11865, 17) | PASS |
| cd_brand membership unchanged | 0 diff | PASS |
| ml_market label-only update | 4 rows | PASS |
| cd_market label-only update | 5 rows | PASS |
| strategic_brand changed data rows | 19 | PASS |
| strategic_product changed data rows | 19 | PASS |
| catalog v0.8 consistency | 75 PASS | PASS |

## 핵심 결과

- strategic_brand: 4,495 row 유지, 4 시장 selective reload 완료.
- strategic_product: 11,865 row 유지, 4 시장 selective reload 완료.
- cd_brand: 2,379 row 유지, membership 변경 0.
- ml_market: 16 row 유지, 4 시장 `source_file_version` + `ingested_at` 만 update.
- cd_market: 19 row 유지, 5 cd `source_file_version` + `ingested_at` 만 update.
- 12 non-target markets: data cell equality PASS.
- catalog v0.8 consistency: 75 PASS.

## 변경 scope

- ml_002 제이클: 수프렙미니에스 class `Trisulfate` → `Sulfate 2종`.
- ml_006 리바로 리바로젯: 에젯토 정 `10/10mg` 중복 → `10/20mg` 정정, SKU 3종 각 1행.
- ml_010 뉴트로진 모빌리아: 듀라스틴 molecule `Tripegfilgrastim` → `PEGFILGRASTIM`.
- ml_012 페린젝트 베노훼럼: strength_pack 16건 numeric 반영.

Commit/tag gate: PASS.
