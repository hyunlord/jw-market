# Layer 2 Dry Run — ml_006

- generated_at: 2026-05-19T13:28:03+09:00
- data_source: ubist
- strategic_product rows: 1,127
- catalog ATC: ['C10A1']
- normalized ATC: ['C10A1']

## UBIST Product Bridge

- match_rule: normalized strategic_product.name OR merge_name == normalized UBIST `제품`
- matched rows: 6,466,168
- matched products: 1,127 / 1,127 (100.00%)
- unmatched products: 0

### Sample Rows

| ml_id | product_id | source | period_yyyymm | raw_rx_amt | raw_rx_cnt | raw_rx_qty | canonical_value | channel | specialty | match_method | match_confidence | source_table | source_row_id | ingested_at |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ml_006 | sp_006_00608_001 | ubist | 2021-01 | 11175142.56 | 397.0 | 16880.88 | 11175142.56 | Semi | Unknown | product_name_exact | high | ubist_parquet | ubist::병원 2021.xlsx::Sheet1::111041::2021-01::642100980 | 2026-05-19T13:28:03+09:00 |
| ml_006 | sp_006_00961_001 | ubist | 2021-01 | 304134.0 | 28.0 | 879.0 | 304134.0 | Semi | Unknown | product_name_exact | high | ubist_parquet | ubist::병원 2021.xlsx::Sheet1::112966::2021-01::628901100 | 2026-05-19T13:28:03+09:00 |
| ml_006 | sp_006_00611_001 | ubist | 2021-01 | 1402130.4 | 49.0 | 2283.6 | 1402130.4 | Semi | Unknown | product_name_exact | high | ubist_parquet | ubist::병원 2021.xlsx::Sheet1::113008::2021-01::628900440 | 2026-05-19T13:28:03+09:00 |
| ml_006 | sp_006_00149_001 | ubist | 2021-01 | 0.0 | 0.0 | 0.0 | 0.0 | Semi | Unknown | product_name_exact | high | ubist_parquet | ubist::병원 2021.xlsx::Sheet1::119296::2021-01::655404170 | 2026-05-19T13:28:03+09:00 |
| ml_006 | sp_006_00970_001 | ubist | 2021-01 | 1893466.8 | 59.0 | 3093.9 | 1893466.8 | Semi | Unknown | product_name_exact | high | ubist_parquet | ubist::병원 2021.xlsx::Sheet1::119478::2021-01::655403160 | 2026-05-19T13:28:03+09:00 |
| ml_006 | sp_006_00624_001 | ubist | 2021-01 | 142396.2 | 4.0 | 215.1 | 142396.2 | Semi | Unknown | product_name_exact | high | ubist_parquet | ubist::병원 2021.xlsx::Sheet1::119809::2021-01::696600920 | 2026-05-19T13:28:03+09:00 |
| ml_006 | sp_006_00153_001 | ubist | 2021-01 | 0.0 | 0.0 | 0.0 | 0.0 | Semi | Unknown | product_name_exact | high | ubist_parquet | ubist::병원 2021.xlsx::Sheet1::121768::2021-01::640904190 | 2026-05-19T13:28:03+09:00 |
| ml_006 | sp_006_00631_001 | ubist | 2021-01 | 85333.2 | 7.0 | 119.85 | 85333.2 | Semi | Unknown | product_name_exact | high | ubist_parquet | ubist::병원 2021.xlsx::Sheet1::122986::2021-01::671704510 | 2026-05-19T13:28:03+09:00 |
| ml_006 | sp_006_00631_001 | ubist | 2021-01 | 0.0 | 0.0 | 0.0 | 0.0 | Semi | Unknown | product_name_exact | high | ubist_parquet | ubist::병원 2021.xlsx::Sheet1::122988::2021-01::671704510 | 2026-05-19T13:28:03+09:00 |
| ml_006 | sp_006_01084_001 | ubist | 2021-01 | 0.0 | 0.0 | 0.0 | 0.0 | Semi | Unknown | product_name_exact | high | ubist_parquet | ubist::병원 2021.xlsx::Sheet1::123191::2021-01::671700660 | 2026-05-19T13:28:03+09:00 |

### Unmatched Products (first 30)

(none)
