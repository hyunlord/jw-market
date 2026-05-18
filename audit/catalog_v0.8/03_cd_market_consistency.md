# 03 cd_market consistency

- Generated at: 2026-05-18 15:07:48
- Repo: `/Users/rexxa/github/jw-market-test`
- Mode: catalog/parquet/xlsx source-level audit
- Catalog competitive_dynamics count: 19
- Parquet cd_market shape: 19 rows × 21 columns
- cd_brand shape: 2379 rows × 16 columns
- Overall status: PASS

| cd_id | name_catalog | name_parquet | ml_id_catalog | ml_id_parquet | cd_filter_catalog | cd_filter_parquet | class_catalog | molecule_catalog | dosage_form_catalog | strength_pack_catalog | nhi_type_catalog | ox_gx_catalog | fish_oil_catalog | class_parquet | molecule_parquet | dosage_form_parquet | strength_pack_parquet | nhi_type_parquet | ox_gx_parquet | fish_oil_parquet | target_iqvia_catalog | target_iqvia_parquet | target_ubist_catalog | target_ubist_parquet | brand_count_catalog | brand_count_cd_brand | status | diff |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cd_001 | 라베칸 라베칸듀오 | 라베칸 라베칸듀오 | ml_001 | ml_001 | cdf_001 | cdf_001 | True | True | False | False | False | False | False | True | True | False | False | False | False | False | [] | [] | ["GH GI", "GH Cardio", "CL IGF"] | ["GH GI", "GH Cardio", "CL IGF"] | 116 | 116 | PASS |  |
| cd_002 | 제이클 | 제이클 | ml_002 | ml_002 | cdf_002 | cdf_002 | True | True | True | False | True | False | False | True | True | True | False | True | False | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | 24 | 24 | PASS |  |
| cd_003 | 가드렛 가드메트 | 가드렛 가드메트 | ml_003 | ml_003 | cdf_003 | cdf_003 | True | True | True | False | False | False | False | True | True | True | False | False | False | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | ["GH Endo", "GH Cardio", "GH Nephro", "CL IGF"] | ["GH Endo", "GH Cardio", "GH Nephro", "CL IGF"] | 18 | 18 | PASS |  |
| cd_004 | 타발리스 | 타발리스 | ml_004 | ml_004 | cdf_004 | cdf_004 | True | True | False | True | False | False | False | True | True | False | True | False | False | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | 10 | 10 | PASS |  |
| cd_005 | 시그마트 | 시그마트 | ml_005 | ml_005 | cdf_005 | cdf_005 | True | True | False | False | False | False | False | True | True | False | False | False | False | False | [] | [] | ["GH Cardio"] | ["GH Cardio"] | 11 | 11 | PASS |  |
| cd_006 | 리바로 리바로젯 | 리바로 리바로젯 | ml_006 | ml_006 | cdf_006 | cdf_006 | True | True | False | True | False | True | False | True | True | False | True | False | True | False | [] | [] | ["GH Cardio", "GH Endo", "GH Neuro", "CL IGF"] | ["GH Cardio", "GH Endo", "GH Neuro", "CL IGF"] | 1095 | 1095 | PASS |  |
| cd_007 | 리바로페노 | 리바로페노 | ml_007 | ml_007 | cdf_007 | cdf_007 | True | True | False | False | False | False | False | True | True | False | False | False | False | False | [] | [] | ["GH Cardio", "GH Endo", "CL IGF"] | ["GH Cardio", "GH Endo", "CL IGF"] | 611 | 611 | PASS |  |
| cd_008 | 리바로하이 | 리바로하이 | ml_008 | ml_008 | cdf_008 | cdf_008 | True | True | False | False | False | False | False | True | True | False | False | False | False | False | [] | [] | ["GH Cardio", "GH Endo", "GH Neuro", "CL IGF"] | ["GH Cardio", "GH Endo", "GH Neuro", "CL IGF"] | 22 | 22 | PASS |  |
| cd_009 | 리바로브이 | 리바로브이 | ml_008 | ml_008 | cdf_009 | cdf_009 | True | True | False | False | False | False | False | True | True | False | False | False | False | False | [] | [] | ["GH Cardio", "GH Endo", "GH Neuro", "CL IGF"] | ["GH Cardio", "GH Endo", "GH Neuro", "CL IGF"] | 26 | 26 | PASS |  |
| cd_010 | 트루패스 | 트루패스 | ml_009 | ml_009 | cdf_010 | cdf_010 | True | True | False | False | False | False | False | True | True | False | False | False | False | False | [] | [] | ["GH Uro", "CL Uro", "CL IGF"] | ["GH Uro", "CL Uro", "CL IGF"] | 160 | 160 | PASS |  |
| cd_011 | 피나스타 제이다트 | 피나스타 제이다트 | ml_009 | ml_009 | cdf_011 | cdf_011 | True | True | False | False | False | False | False | True | True | False | False | False | False | False | [] | [] | ["GH Uro", "CL Uro", "CL IGF"] | ["GH Uro", "CL Uro", "CL IGF"] | 140 | 140 | PASS |  |
| cd_012 | 뉴트로진 | 뉴트로진 | ml_010 | ml_010 | cdf_012 | cdf_012 | True | True | False | False | True | False | False | True | True | False | False | True | False | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | 8 | 8 | PASS |  |
| cd_013 | 모빌리아 | 모빌리아 | ml_010 | ml_010 | cdf_013 | cdf_013 | True | True | False | False | True | False | False | True | True | False | False | True | False | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | 2 | 2 | PASS |  |
| cd_014 | 악템라 | 악템라 | ml_011 | ml_011 | cdf_014 | cdf_014 | True | True | False | False | False | True | False | True | True | False | False | False | True | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | 26 | 26 | PASS |  |
| cd_015 | 페린젝트 베노훼럼 | 페린젝트 베노훼럼 | ml_012 | ml_012 | cdf_015 | cdf_015 | True | True | True | True | True | False | False | True | True | True | True | True | False | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | 16 | 16 | PASS |  |
| cd_016 | 헴리브라 | 헴리브라 | ml_013 | ml_013 | cdf_016 | cdf_016 | True | True | False | False | True | False | False | True | True | False | False | True | False | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | 14 | 14 | PASS |  |
| cd_017 | 엔커버 | 엔커버 | ml_015 | ml_015 | cdf_017 | cdf_017 | False | True | False | True | True | False | False | False | True | False | True | True | False | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | 4 | 4 | PASS |  |
| cd_018 | 위너프 위너프에이플러스 | 위너프 위너프에이플러스 | ml_014 | ml_014 | cdf_018 | cdf_018 | True | True | True | True | True | False | True | True | True | True | True | True | False | True | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | 68 | 68 | PASS |  |
| cd_019 | 플라주오피 | 플라주오피 | ml_016 | ml_016 | cdf_019 | cdf_019 | True | True | False | True | True | False | False | True | True | False | True | True | False | False | ["KHPA", "KCPA", "KPA"] | ["KHPA", "KCPA", "KPA"] | [] | [] | 8 | 8 | PASS |  |
