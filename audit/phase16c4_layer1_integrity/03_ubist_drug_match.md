# 03. UBIST 약품코드 Matching Probe

Direct `strategic_product.약품코드 ∩ UBIST.약품코드` is unsupported because `strategic_product` has no `약품코드` column.

## Candidate Bracket-Code Match

| Metric | Value |
|---|---:|
| strategic products with bracket code | 7,887 |
| bracket-code products found in UBIST `성분용량` | 7,887 |
| candidate match rate | 100.00% |

## By ml_id

| ml_id  | strategic_products_with_bracket_code | strategic_products_bracket_code_in_ubist |
| ------ | ------------------------------------ | ---------------------------------------- |
| ml_001 | 1,411                                | 1,411                                    |
| ml_005 | 808                                  | 808                                      |
| ml_006 | 79                                   | 79                                       |
| ml_007 | 983                                  | 983                                      |
| ml_008 | 3,547                                | 3,547                                    |
| ml_009 | 1,059                                | 1,059                                    |

## ATC-Based Coverage Estimate

| ml_id  | ml_name        | atc_codes    | ubist_atc_rows | ubist_atc_unique_drugs |
| ------ | -------------- | ------------ | -------------- | ---------------------- |
| ml_001 | 라베칸 라베칸듀오      | A02B2        | 0              | 0                      |
| ml_002 | 제이클            | A06B2        | 0              | 0                      |
| ml_003 | 가드렛 가드메트       | A10N1, A10N3 | 1,950,907      | 694                    |
| ml_004 | 타발리스           | B02E9        | 0              | 0                      |
| ml_005 | 시그마트           | C01D0        | 0              | 0                      |
| ml_006 | 리바로 리바로젯       | C10A1        | 3,780,217      | 756                    |
| ml_007 | 리바로페노          | C10A1, C10A3 | 3,786,397      | 757                    |
| ml_008 | 리바로하이 리바로브이    | C10A1, C09B3 | 3,780,217      | 756                    |
| ml_009 | 트루패스 피나스타 제이다트 | G04C0        | 0              | 0                      |
| ml_010 | 뉴트로진 모빌리아      | L03A1        | 0              | 0                      |
| ml_011 | 악템라            | L04B0        | 0              | 0                      |
| ml_012 | 페린젝트 베노훼럼      | B03A1        | 0              | 0                      |
| ml_013 | 헴리브라           | B02D1, B02D2 | 0              | 0                      |
| ml_014 | 위너프 위너프에이플러스   | K01D2, K01E0 | 0              | 0                      |
| ml_015 | 엔커버            | V06D0        | 0              | 0                      |
| ml_016 | 플라주오피          | K01A3, K01A1 | 0              | 0                      |

Supporting CSV files: `ubist_catalog_atc_coverage.csv`, `ubist_bracket_code_match_by_ml.csv`.
