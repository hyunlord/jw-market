# ETL config

이 폴더는 ETL에서 사용하는 git-tracked 설정을 둔다.

## Files

- `expected_row_counts.yaml` - s2 catalog validation row-count invariants.
- `master_column_mapping_catalog.md` - s2-a master extract column mapping reference.
- `market_metadata.yaml` - archive `catalog/market_metadata.yaml` byte copy for the future layer2 bridge.
- `customer_dictionary.yaml` - archive `catalog/customer_dictionary.yaml` byte copy for the future layer2 bridge.
- `market_overrides.yaml` - archive `catalog/market_overrides.yaml` byte copy retained for layer2 parity.
- `enrichment_rules.yaml` - archive `catalog/enrichment_rules.yaml` byte copy retained for layer2 parity.

The four catalog YAML files were copied without content edits. s3-a will wire layer2 to read them from this config directory.
