# Staging Schema — Phase 1

Generated: 2026-05-09

## 0. Scope

This document is the Phase 1 schema definition summary. Phase 1 defines DDL only.

Excluded from Phase 1:

- staging row loading
- mart tables
- mock JSON
- API or screen outputs

Source-of-truth DDL files:

- `sql/schema_master.sql`
- `sql/schema_iqvia.sql`
- `sql/schema_ubist.sql`

## 1. Table Summary

| Area | Table | Primary key | Main JSON columns |
| --- | --- | --- | --- |
| Master | `stg_master_drug` | `strategic_market_id, drug_index` | `drug_extra_json`, `raw_row_json`, `column_metadata_json` |
| Master | `stg_master_market_definition` | `strategic_market_id` | `full_market_atc4_codes_json`, `direct_competition_brands_json`, `analysis_levels_json`, `target_customer_priority_json`, `raw_row_json` |
| Master | `stg_master_qa` | `qa_id` | `application_actions_json` |
| Master | `stg_master_brand_consolidation` | `strategic_market_id, brand_group, member_drug_index` | none |
| Master | `stg_master_mapping_table` | `mapping_id` | none |
| IQVIA | `stg_iqvia_nsa_raw` | `product_key` | `observations_json`, `raw_extra_json` |
| IQVIA | `stg_iqvia_chso_raw` | `product_key` | `observations_json`, `raw_extra_json` |
| IQVIA | `stg_iqvia_csd_channel_raw` | `csd_row_id` | `observations_json`, `raw_row_json` |
| IQVIA | `stg_iqvia_csd_keywords_raw` | `csd_row_id` | `raw_row_json` |
| IQVIA | `stg_iqvia_csd_meetings_raw` | `csd_row_id` | `raw_row_json` |
| UBIST | `stg_ubist_raw` | `product_key` | `observations_json`, `raw_extra_json` |

Total: 11 staging tables.

## 2. Master Tables

### `stg_master_drug`

Drug-level Master mapping rows keyed by `strategic_market_id` and workbook-local `drug_index`.

Important choices:

- Standard columns preserve market, drug, classification, formulation, strength, and manual mapping outputs.
- `column_metadata_json` records raw/manual/overlay origin for each promoted standard column.
- `drug_extra_json` keeps sheet-specific promoted details such as generation, fish oil flag, or Fe content.
- `raw_row_json` keeps the Master row shape for traceability.
- No Master overlay is applied in staging.

### `stg_master_market_definition`

Market-level definition table for 16 strategic markets.

Important JSON fields:

- `full_market_atc4_codes_json`
- `direct_competition_brands_json`
- `analysis_levels_json`
- `target_customer_priority_json`

### `stg_master_qa`

Q&A and user decision rows that may drive future Master processing rules.

### `stg_master_brand_consolidation`

Brand grouping table for markets such as 악템라 where a visible brand group differs from raw product rows.

### `stg_master_mapping_table`

Manual recode table for traceable mappings such as class recode, molecule recode, or 리바로하이 mapping outputs.

## 3. IQVIA Tables

### `stg_iqvia_nsa_raw`

NSA product/channel grain table.

Product key components:

```text
audit_code + mfr_code + product_name + pack_desc + strength + nhi_type + molecule_desc
```

Important choices:

- `audit_code` is always preserved.
- `is_grand_total` flags the Grand Total row without dropping it.
- Phase 3a policy stages a 4Q + 2Q union:
  - 2Q metadata wins for common products.
  - 4Q-only relational metadata (`PACK SIZE`, `PRODUCT AGE`, `PACK AGE`) is not promoted.
  - `audit_desc` is promoted from 2Q or derived from `audit_code` for 4Q-only rows.
- `observations_json` stores quarterly metric observations only:
  - 22-quarter range where available: `2020-Q3` through `2025-Q4`.
  - Metrics: `value_lc`, `units`, `counting_units`, `dosage_units`, and `price`.
  - 2Q `price` is raw; 4Q `price` is temporary inverse-derived as `Values LC / Units`.
- Annual and MAT observations are intentionally not staged because they are derivable from quarters.
- `raw_extra_json` preserves source fields not promoted to dedicated columns.
- `source_files` records whether the row came from `2q_csv`, `4q_csv`, or `4q_csv|2q_csv`.
- `meta_source` records whether metadata came from 2Q priority logic or a 4Q-only row.

### `stg_iqvia_chso_raw`

CHSO schema-only table. Phase 1 defines the shape, but CHSO is not in the Phase 2-4 first loading lane unless a later decision changes that.

Product key components:

```text
audit_desc + mfr_name_kor + product_name_kor + pack_description + atc4 + chc4
```

`observations_json` stores monthly Sell-Out values using the raw month headers.

### `stg_iqvia_csd_channel_raw`

ChannelDynamics raw staging table for monthly channel/product/market sheets.

`observations_json` stores time-series metrics. `raw_row_json` preserves full source-row detail because CSD sheet layouts vary.

### `stg_iqvia_csd_keywords_raw`

Keywords raw staging table. Phase 1 keeps source rows as JSON because downstream usage is not normalized yet.

### `stg_iqvia_csd_meetings_raw`

Meetings raw staging table. Phase 1 keeps source rows as JSON because downstream usage is not normalized yet.

## 4. UBIST Table

### `stg_ubist_raw`

UBIST row grain includes product and all row-level split dimensions:

```text
drug_code + hosp_type + specialty + age_group + gender
```

Important choices:

- `hosp_type` stores raw `종별` unchanged.
- File/folder name is not used as the canonical channel value.
- Duplicated UBIST metadata columns are preserved with `_2` columns where Phase 0 found repeated headers.
- `observations_json` stores monthly metrics only: `rx_amt`, `rx_cnt`, and `rx_qty`.

## 5. JSON Structures

### IQVIA NSA `observations_json`

```json
{
  "2025-Q4": {
    "value_lc": 35160000000,
    "units": 123,
    "counting_units": 456,
    "dosage_units": 789,
    "price": 1000
  }
}
```

### IQVIA CHSO `observations_json`

```json
{
  "2026-01": {
    "value_lc_si_price": 123456789
  }
}
```

### UBIST `observations_json`

```json
{
  "2026-02": {
    "rx_amt": 8724327374,
    "rx_cnt": 1230,
    "rx_qty": 3100
  }
}
```

### Master `column_metadata_json`

See `docs/reference/master_column_mapping_catalog.md` for 16 market-specific instances.

## 6. DDL Files

The executable DDL is intentionally kept in `sql/schema_*.sql` to avoid drift between documentation and database contracts.
