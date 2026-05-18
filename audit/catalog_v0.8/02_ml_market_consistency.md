# 02 ml_market consistency

- Generated at: 2026-05-18 15:07:48
- Repo: `/Users/rexxa/github/jw-market-test`
- Mode: catalog/parquet/xlsx source-level audit
- Catalog markets count: 16
- Parquet ml_market shape: 16 rows × 19 columns
- Overall status: PASS

| ml_id | name_catalog | name_parquet | data_source_catalog | data_source_parquet | class_catalog | molecule_catalog | dosage_form_catalog | strength_pack_catalog | nhi_type_catalog | ox_gx_catalog | fish_oil_catalog | class_parquet | molecule_parquet | dosage_form_parquet | strength_pack_parquet | nhi_type_parquet | ox_gx_parquet | fish_oil_parquet | target_iqvia_catalog | target_iqvia_parquet | target_ubist_catalog | target_ubist_parquet | status | diff |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ml_001 | 라베칸 라베칸듀오 | 라베칸 라베칸듀오 | ubist | ubist | True | True | False | False | False | False | False | True | True | False | False | False | False | False | [] | [] | ["GH GI", "GH Cardio", "CL IGF"] | ["GH GI", "GH Cardio", "CL IGF"] | PASS |  |
| ml_002 | 제이클 | 제이클 | iqvia | iqvia | True | True | True | False | True | False | False | True | True | True | False | True | False | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | PASS |  |
| ml_003 | 가드렛 가드메트 | 가드렛 가드메트 | both | both | True | True | True | False | False | False | False | True | True | True | False | False | False | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | ["GH Endo", "GH Cardio", "GH Nephro", "CL IGF"] | ["GH Endo", "GH Cardio", "GH Nephro", "CL IGF"] | PASS |  |
| ml_004 | 타발리스 | 타발리스 | iqvia | iqvia | True | True | False | True | False | False | False | True | True | False | True | False | False | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | PASS |  |
| ml_005 | 시그마트 | 시그마트 | ubist | ubist | True | True | False | False | False | False | False | True | True | False | False | False | False | False | [] | [] | ["GH Cardio"] | ["GH Cardio"] | PASS |  |
| ml_006 | 리바로 리바로젯 | 리바로 리바로젯 | ubist | ubist | True | True | False | True | False | True | False | True | True | False | True | False | True | False | [] | [] | ["GH Cardio", "GH Endo", "GH Neuro", "CL IGF"] | ["GH Cardio", "GH Endo", "GH Neuro", "CL IGF"] | PASS |  |
| ml_007 | 리바로페노 | 리바로페노 | ubist | ubist | True | True | False | False | False | False | False | True | True | False | False | False | False | False | [] | [] | ["GH Cardio", "GH Endo", "CL IGF"] | ["GH Cardio", "GH Endo", "CL IGF"] | PASS |  |
| ml_008 | 리바로하이 리바로브이 | 리바로하이 리바로브이 | ubist | ubist | True | True | False | False | False | False | False | True | True | False | False | False | False | False | [] | [] | ["GH Cardio", "GH Endo", "GH Neuro", "CL IGF"] | ["GH Cardio", "GH Endo", "GH Neuro", "CL IGF"] | PASS |  |
| ml_009 | 트루패스 피나스타 제이다트 | 트루패스 피나스타 제이다트 | ubist | ubist | True | True | False | False | False | False | False | True | True | False | False | False | False | False | [] | [] | ["GH Uro", "CL Uro", "CL IGF"] | ["GH Uro", "CL Uro", "CL IGF"] | PASS |  |
| ml_010 | 뉴트로진 모빌리아 | 뉴트로진 모빌리아 | iqvia | iqvia | True | True | False | False | True | False | False | True | True | False | False | True | False | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | PASS |  |
| ml_011 | 악템라 | 악템라 | iqvia | iqvia | True | True | False | False | False | True | False | True | True | False | False | False | True | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | PASS |  |
| ml_012 | 페린젝트 베노훼럼 | 페린젝트 베노훼럼 | iqvia | iqvia | True | True | True | True | True | False | False | True | True | True | True | True | False | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | PASS |  |
| ml_013 | 헴리브라 | 헴리브라 | iqvia | iqvia | True | True | False | False | True | False | False | True | True | False | False | True | False | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | PASS |  |
| ml_014 | 위너프 위너프에이플러스 | 위너프 위너프에이플러스 | iqvia | iqvia | True | True | True | True | True | False | True | True | True | True | True | True | False | True | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | PASS |  |
| ml_015 | 엔커버 | 엔커버 | both | both | False | True | False | True | True | False | False | False | True | False | True | True | False | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | PASS |  |
| ml_016 | 플라주오피 | 플라주오피 | iqvia | iqvia | True | True | False | True | True | False | False | True | True | False | True | True | False | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | PASS |  |
