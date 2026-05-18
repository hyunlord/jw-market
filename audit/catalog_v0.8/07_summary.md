# 07 summary

- Generated at: 2026-05-18 15:07:48
- Repo: `/Users/rexxa/github/jw-market-test`
- Mode: catalog/parquet/xlsx source-level audit
- Overall audit status: PASS
- Catalog dir: `/Users/rexxa/github/jw-market-test/catalog`
- Audit zip: `catalog_v0.8_audit_20260518_1507.zip`

## Status counts
| status | count |
| --- | --- |
| PASS | 75 |

## Check list
| check | status | detail |
| --- | --- | --- |
| catalog_file::README.md | PASS | N/A |
| catalog_file::enrichment_rules.yaml | PASS | PASS |
| catalog_file::customer_dictionary.yaml | PASS | PASS |
| catalog_file::market_metadata.yaml | PASS | PASS |
| catalog_file::market_overrides.yaml | PASS | PASS |
| ml_market::ml_001 | PASS |  |
| ml_market::ml_002 | PASS |  |
| ml_market::ml_003 | PASS |  |
| ml_market::ml_004 | PASS |  |
| ml_market::ml_005 | PASS |  |
| ml_market::ml_006 | PASS |  |
| ml_market::ml_007 | PASS |  |
| ml_market::ml_008 | PASS |  |
| ml_market::ml_009 | PASS |  |
| ml_market::ml_010 | PASS |  |
| ml_market::ml_011 | PASS |  |
| ml_market::ml_012 | PASS |  |
| ml_market::ml_013 | PASS |  |
| ml_market::ml_014 | PASS |  |
| ml_market::ml_015 | PASS |  |
| ml_market::ml_016 | PASS |  |
| ml_market::key_set | PASS | ml_001..ml_016 |
| cd_market::cd_001 | PASS |  |
| cd_market::cd_002 | PASS |  |
| cd_market::cd_003 | PASS |  |
| cd_market::cd_004 | PASS |  |
| cd_market::cd_005 | PASS |  |
| cd_market::cd_006 | PASS |  |
| cd_market::cd_007 | PASS |  |
| cd_market::cd_008 | PASS |  |
| cd_market::cd_009 | PASS |  |
| cd_market::cd_010 | PASS |  |
| cd_market::cd_011 | PASS |  |
| cd_market::cd_012 | PASS |  |
| cd_market::cd_013 | PASS |  |
| cd_market::cd_014 | PASS |  |
| cd_market::cd_015 | PASS |  |
| cd_market::cd_016 | PASS |  |
| cd_market::cd_017 | PASS |  |
| cd_market::cd_018 | PASS |  |
| cd_market::cd_019 | PASS |  |
| cd_market::key_set | PASS | cd_001..cd_019 |
| ml_cd_mapping::ml_001 | PASS | ['cd_001'] == ['cd_001'] |
| ml_cd_mapping::ml_002 | PASS | ['cd_002'] == ['cd_002'] |
| ml_cd_mapping::ml_003 | PASS | ['cd_003'] == ['cd_003'] |
| ml_cd_mapping::ml_004 | PASS | ['cd_004'] == ['cd_004'] |
| ml_cd_mapping::ml_005 | PASS | ['cd_005'] == ['cd_005'] |
| ml_cd_mapping::ml_006 | PASS | ['cd_006'] == ['cd_006'] |
| ml_cd_mapping::ml_007 | PASS | ['cd_007'] == ['cd_007'] |
| ml_cd_mapping::ml_008 | PASS | required_special=['cd_008', 'cd_009'] special_status=PASS |
| ml_cd_mapping::ml_009 | PASS | required_special=['cd_010', 'cd_011'] special_status=PASS |
| ml_cd_mapping::ml_010 | PASS | required_special=['cd_012', 'cd_013'] special_status=PASS |
| ml_cd_mapping::ml_011 | PASS | ['cd_014'] == ['cd_014'] |
| ml_cd_mapping::ml_012 | PASS | required_special=['cd_015'] special_status=PASS |
| ml_cd_mapping::ml_013 | PASS | ['cd_016'] == ['cd_016'] |
| ml_cd_mapping::ml_014 | PASS | required_special=['cd_018'] special_status=PASS |
| ml_cd_mapping::ml_015 | PASS | required_special=['cd_017'] special_status=PASS |
| ml_cd_mapping::ml_016 | PASS | ['cd_019'] == ['cd_019'] |
| detail_sheets::sheet_04 | PASS |  |
| detail_sheets::sheet_05 | PASS |  |
| detail_sheets::sheet_06 | PASS |  |
| detail_sheets::sheet_07 | PASS |  |
| detail_sheets::sheet_08 | PASS |  |
| detail_sheets::sheet_09 | PASS |  |
| detail_sheets::sheet_10 | PASS |  |
| detail_sheets::sheet_11 | PASS |  |
| detail_sheets::sheet_12 | PASS |  |
| detail_sheets::sheet_13 | PASS |  |
| detail_sheets::sheet_14 | PASS |  |
| detail_sheets::sheet_15 | PASS |  |
| detail_sheets::sheet_16 | PASS |  |
| detail_sheets::sheet_17 | PASS |  |
| detail_sheets::sheet_18 | PASS |  |
| detail_sheets::sheet_19 | PASS |  |
| detail_sheets::key_set | PASS | sheet_04..sheet_19 |

## Recommended next work
- PL decision: whether Phase 14 parquet `source_file_version` should be updated from 260422 to 260518.
- Separate audit: strategic_brand / strategic_product entry changes between 260422 and 260518.
- Proceed to Layer 1 staging only after PL decision.
