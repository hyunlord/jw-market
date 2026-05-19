# 02. Strategic Brand/Product Columns and Matching Candidates

- `strategic_brand`: 4,495 rows x 16 cols
- `strategic_product`: 11,865 rows x 17 cols

`strategic_product` columns:

```text
['product_id', 'name', 'merge_name', 'brand_id', 'ml_id', 'cd_id', 'class', 'molecule', 'dosage_form', 'strength_pack', 'nhi_type', 'ox_gx', 'fish_oil', '판매사', '제조사', 'source_file_version', 'ingested_at']
```

## Direct Key Availability

| source    | direct_key_expected           | strategic_product_column_present | present_columns | audit_decision                                                       |
| --------- | ----------------------------- | -------------------------------- | --------------- | -------------------------------------------------------------------- |
| UBIST     | 약품코드                          | 0                                |                 | direct join not available in current strategic_product schema        |
| IQVIA NSA | AUDIT_CODE / IQVIA audit code | 0                                |                 | direct audit_code join not available; NSA audit_code is channel code |

Source-level interpretation:
- UBIST exposes `약품코드`, but `strategic_product` does not carry it.
- IQVIA NSA `audit_code` values are channel/customer codes (`KHPA`, `KCPA`, `KCPA_DIRECT`, `KPA`), not product identifiers.
- Layer 2 should use a formal bridge or deterministic product metadata matching, not assume direct keys already exist.

Supporting CSV: `strategic_column_inventory.csv`.
