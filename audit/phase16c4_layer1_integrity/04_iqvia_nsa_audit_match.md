# 04. IQVIA NSA Audit-Code Matching Probe

Direct `strategic_product.AUDIT_CODE ∩ iqvia_nsa_quarterly_raw.audit_code` is unsupported because `strategic_product` has no `AUDIT_CODE` column. NSA `audit_code` is a channel/customer code.

## audit_code Distribution

| audit_code  | canonical_audit_channel | row_count |
| ----------- | ----------------------- | --------- |
| KPA         | KPA                     | 1,778,057 |
| KHPA        | KHPA                    | 716,839   |
| KCPA_DIRECT | KCPA                    | 182,438   |
| Grand Total | Grand Total             | 60        |

## Candidate Product-Key Payload Fields

| product_name_kor | pack_desc              | mfr_name_kor | atc_code | row_count |
| ---------------- | ---------------------- | ------------ | -------- | --------- |
| 리도카인 알리코         | GEL 2% 120G            | 알리코제약        | N01B3    | 180       |
| 프롤리아             | PRE-F SRN SC 60MG 1ML  | 암젠코리아        | M05B9    | 180       |
| 리트모놈SR           | CAPS L.A 225MG 50      | 한국애보트        | C01B0    | 180       |
| 데파코트             | C.T FILM L.A 250MG 100 | 한국애보트        | N03A0    | 180       |
| 데파코트             | C.T FILM L.A 500MG 100 | 한국애보트        | N03A0    | 180       |
| 라벤다 에이프로젠        | CREAM 15G              | 에이프로젠        | D07B3    | 180       |
| 베라실              | C.TAB FILM 20Y 100     | 한국아스텔라스제약    | B01C4    | 180       |
| 하루날디             | FR-DRIED TAB 0.2MG 140 | 한국아스텔라스제약    | G04C2    | 180       |
| 시네츄라             | SYR 500ML              | 안국약품         | R05C0    | 180       |
| 알카인              | EYE DROPS 0.5% 15ML    | 한국알콘         | S01H0    | 180       |
| 레바미피드 알보젠        | C.TAB FILM 100MG 500   | 알보젠          | A02B9    | 180       |
| 맥스디오             | C.TAB FILM 160MG 100   | 알보젠          | C09C0    | 180       |
| 시나세트             | C.TAB FILM 25MG 100    | 알보젠          | H04F0    | 180       |
| 카리메트 알보젠         | GRANS SACHET 5G 100    | 알보젠          | V03G1    | 180       |
| 카리메트 알보젠         | PWD SACHET 5G 100      | 알보젠          | V03G1    | 180       |
| 네프로              | TABS 710MG 300         | 알보젠          | V03G2    | 180       |
| 아타칸              | TABS 16MG 100          | 아스트라제네카      | C09C0    | 180       |
| 아타칸              | TABS 8MG 100           | 아스트라제네카      | C09C0    | 180       |
| 풀미코트레스퓰          | LIQ INH 0.5MG 2ML 30   | 아스트라제네카      | R03D1    | 180       |
| 트라젠타             | C.TAB FILM 5MG 30      | 한국베링거인겔하임    | A10N1    | 180       |

## ATC + Target Channel Coverage Estimate

| ml_id  | ml_name        | atc_codes    | target_iqvia    | nsa_atc_rows_all_channels | nsa_atc_target_channel_rows | nsa_unique_product_pack_keys_target |
| ------ | -------------- | ------------ | --------------- | ------------------------- | --------------------------- | ----------------------------------- |
| ml_001 | 라베칸 라베칸듀오      | A02B2        |                 | 52,598                    | 0                           | 0                                   |
| ml_002 | 제이클            | A06B2        | KHPA, KCPA, KPA | 1,926                     | 1,926                       | 65                                  |
| ml_003 | 가드렛 가드메트       | A10N1, A10N3 | KHPA, KCPA, KPA | 30,495                    | 30,495                      | 1,298                               |
| ml_004 | 타발리스           | B02E9        | KHPA, KCPA, KPA | 20                        | 20                          | 4                                   |
| ml_005 | 시그마트           | C01D0        |                 | 3,429                     | 0                           | 0                                   |
| ml_006 | 리바로 리바로젯       | C10A1        |                 | 78,943                    | 0                           | 0                                   |
| ml_007 | 리바로페노          | C10A1, C10A3 |                 | 79,048                    | 0                           | 0                                   |
| ml_008 | 리바로하이 리바로브이    | C10A1, C09B3 |                 | 79,237                    | 0                           | 0                                   |
| ml_009 | 트루패스 피나스타 제이다트 | G04C0        |                 | 0                         | 0                           | 0                                   |
| ml_010 | 뉴트로진 모빌리아      | L03A1        | KHPA, KCPA, KPA | 1,192                     | 1,192                       | 34                                  |
| ml_011 | 악템라            | L04B0        | KHPA, KCPA, KPA | 1,927                     | 1,927                       | 48                                  |
| ml_012 | 페린젝트 베노훼럼      | B03A1        | KHPA, KCPA, KPA | 3,816                     | 3,816                       | 82                                  |
| ml_013 | 헴리브라           | B02D1, B02D2 | KHPA, KCPA, KPA | 3,363                     | 3,363                       | 89                                  |
| ml_014 | 위너프 위너프에이플러스   | K01D2, K01E0 | KHPA, KCPA, KPA | 4,607                     | 4,607                       | 140                                 |
| ml_015 | 엔커버            | V06D0        | KHPA, KCPA, KPA | 583                       | 583                         | 10                                  |
| ml_016 | 플라주오피          | K01A3, K01A1 | KHPA, KCPA, KPA | 1,633                     | 1,633                       | 44                                  |

Supporting CSV files: `nsa_audit_code_distribution.csv`, `nsa_product_pack_sample.csv`, `nsa_atc_channel_coverage.csv`.
