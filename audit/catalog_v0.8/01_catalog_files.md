# 01 catalog files integrity

- Generated at: 2026-05-18 15:07:48
- Repo: `/Users/rexxa/github/jw-market-test`
- Mode: catalog/parquet/xlsx source-level audit
- Catalog dir: `/Users/rexxa/github/jw-market-test/catalog`
- Expected files: `['README.md', 'enrichment_rules.yaml', 'customer_dictionary.yaml', 'market_metadata.yaml', 'market_overrides.yaml']`

| file | exists | size_bytes | yaml_parse | top_level_keys | status |
| --- | --- | --- | --- | --- | --- |
| README.md | True | 4504 | N/A |  | PASS |
| enrichment_rules.yaml | True | 5483 | PASS | ["exclude_rules", "column_rules", "source_columns"] | PASS |
| customer_dictionary.yaml | True | 3818 | PASS | ["ubist_channel", "ubist_channel_reverse", "ubist_specialty", "ubist_specialty_reverse", "iqvia_channel"] | PASS |
| market_metadata.yaml | True | 23466 | PASS | ["counts", "markets", "competitive_dynamics", "ml_cd_mapping", "detail_sheets", "metric_definitions", "source_files"] | PASS |
| market_overrides.yaml | True | 11374 | PASS | ["ml_001_rabecan", "ml_002_jcle", "ml_003_gadrate", "ml_004_tavalisse", "ml_005_sigmart", "ml_006_livalo", "ml_007_livafeno", "ml_008_livalohigh_livalov", "ml_009_trupass_pinasta_jdt", "ml_010_neutrogin_mobilia", "ml_011_actemra", "ml_012_ferinject_venoferum", "ml_013_hemlibra", "ml_014_winuf", "ml_015_encover", "ml_016_plazoopi", "cd_filter_logic", "cd_id_swap"] | PASS |
