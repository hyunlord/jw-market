# 04 ml_cd_mapping check

- Generated at: 2026-05-18 15:07:48
- Repo: `/Users/rexxa/github/jw-market-test`
- Mode: catalog/parquet/xlsx source-level audit
- Mapping source in catalog: `market_metadata.yaml::ml_cd_mapping`
- Mapping source in parquet: `cd_market.ml_id` grouped to cd_id list
- Special required mappings checked: ml_008 split, ml_009 split, ml_010 split, ml_012 collapse, ml_014/ml_015 swap.

| ml_id | catalog_cd_ids | parquet_cd_ids | status | note |
| --- | --- | --- | --- | --- |
| ml_001 | ["cd_001"] | ["cd_001"] | PASS |  |
| ml_002 | ["cd_002"] | ["cd_002"] | PASS |  |
| ml_003 | ["cd_003"] | ["cd_003"] | PASS |  |
| ml_004 | ["cd_004"] | ["cd_004"] | PASS |  |
| ml_005 | ["cd_005"] | ["cd_005"] | PASS |  |
| ml_006 | ["cd_006"] | ["cd_006"] | PASS |  |
| ml_007 | ["cd_007"] | ["cd_007"] | PASS |  |
| ml_008 | ["cd_008", "cd_009"] | ["cd_008", "cd_009"] | PASS | required_special=['cd_008', 'cd_009'] special_status=PASS |
| ml_009 | ["cd_010", "cd_011"] | ["cd_010", "cd_011"] | PASS | required_special=['cd_010', 'cd_011'] special_status=PASS |
| ml_010 | ["cd_012", "cd_013"] | ["cd_012", "cd_013"] | PASS | required_special=['cd_012', 'cd_013'] special_status=PASS |
| ml_011 | ["cd_014"] | ["cd_014"] | PASS |  |
| ml_012 | ["cd_015"] | ["cd_015"] | PASS | required_special=['cd_015'] special_status=PASS |
| ml_013 | ["cd_016"] | ["cd_016"] | PASS |  |
| ml_014 | ["cd_018"] | ["cd_018"] | PASS | required_special=['cd_018'] special_status=PASS |
| ml_015 | ["cd_017"] | ["cd_017"] | PASS | required_special=['cd_017'] special_status=PASS |
| ml_016 | ["cd_019"] | ["cd_019"] | PASS |  |
