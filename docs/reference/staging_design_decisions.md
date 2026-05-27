# Staging Design Decisions — Phase 1

Generated: 2026-05-09

## Decision 1: Product keys include every staging split dimension

Status: `VALID`

The Phase 1 schema defines product keys at the raw staging grain, not at a display or mart grain.

| Staging table | Key components |
| --- | --- |
| `stg_iqvia_nsa_raw` | `audit_code + mfr_code + product_name + pack_desc + strength + nhi_type + molecule_desc` |
| `stg_iqvia_chso_raw` | `audit_desc + mfr_name_kor + product_name_kor + pack_description + atc4 + chc4` |
| `stg_iqvia_csd_channel_raw` | `source_file + source_sheet + source_row_id` |
| `stg_iqvia_csd_keywords_raw` | `source_file + source_sheet + source_row_id` |
| `stg_iqvia_csd_meetings_raw` | `source_file + source_sheet + source_row_id` |
| `stg_ubist_raw` | `drug_code + hosp_type + specialty + age_group + gender` |
| `stg_master_drug` | `strategic_market_id + drug_index` |

Rationale:

- Phase 1 policies explicitly require AUDIT CODE channel separation for NSA.
- UBIST `종별`, `진료과`, `연령`, and `성별` are row-grain split dimensions.
- CSD layouts vary by workbook and sheet, so source row identity is the stable raw key.

## Decision 2: NSA AUDIT CODE is preserved and Grand Total is flagged

Status: `VALID`

`stg_iqvia_nsa_raw` keeps all four raw AUDIT CODE groups:

- `KCPA_DIRECT`
- `KHPA`
- `KPA`
- `Grand Total`

`is_grand_total` is a required boolean so downstream queries can explicitly include or exclude the Grand Total row.

Rationale:

- Phase 1 policy P-2 selected option 3: stage all rows and mark Grand Total.
- Category story material requires KHPA/KCPA/KPA channel separation.
- Dropping Grand Total in staging would destroy raw traceability.

## Decision 3: Prevent the v2.6 NSA key collision pattern

Status: `VALID`

The Phase 1 request records a v2.6 failure mode: product key construction omitted AUDIT CODE, causing rows from different channels to collapse into one product record. The reported symptom was severe metric loss for products where channel rows should have remained separate.

Phase 1 prevention:

1. `audit_code` is a required column in `stg_iqvia_nsa_raw`.
2. `audit_code` is part of the NSA product key components.
3. `idx_nsa_audit_code` supports channel-scoped validation and query paths.
4. A future loading phase must verify that the same product in different AUDIT CODE groups produces different keys.

## Decision 4: UBIST `종별` is row grain, not file metadata

Status: `VALID`

`stg_ubist_raw.hosp_type` stores the raw `종별` value unchanged. Folder or file names such as `종병` are not treated as authoritative channel values.

Rationale:

- Phase 1 policy P-8 requires row-level `종별` separation.
- The category/dashboard materials treat institution type as an analysis dimension.
- `product_key` includes `hosp_type`, `specialty`, `age_group`, and `gender` to avoid collapsing demographic or institution-level rows.

## Decision 5: Observations JSON stores time-series metrics only

Status: `VALID`

`observations_json` is limited to time observations:

- NSA: quarter keys with value, unit, dosage/counting unit, and price metrics.
- CHSO: month keys with Sell-Out value metrics.
- UBIST: month keys with `rx_amt`, `rx_cnt`, and `rx_qty`.

Split dimensions such as AUDIT CODE, `종별`, 진료과, age, and gender stay as relational columns.

Rationale:

- Keeping split dimensions out of JSON prevents grain ambiguity.
- Raw metric columns can expand over time without changing DDL for every new period.

## Decision 6: Master overlay is metadata, not a staging mutation

Status: `VALID`

Master rows preserve standard columns and `column_metadata_json`; later Strategic View logic may apply `master_first` overlay rules.

Rationale:

- Phase 1 policy P-7 says staging preserves raw and Master values.
- Overlay is a view/mart concern, not a Phase 1 DDL mutation.
- `master_column_mapping_catalog.md` records the per-market `column_metadata_json` intent.

## Decision 7: CHSO is schema-only in Phase 1

Status: `VALID`

`stg_iqvia_chso_raw` is defined from `raw_iqvia_chso_columns.csv`, but CHSO is not in the first loading lane unless a future phase reopens that decision.

Rationale:

- Phase 1 policy P-3 says CHSO is excluded from the first loading target.
- Defining the table now keeps the contract visible without staging rows.

## Decision 8: CSD has three equal raw staging families

Status: `VALID`

Phase 1 defines three CSD staging tables:

- `stg_iqvia_csd_channel_raw`
- `stg_iqvia_csd_keywords_raw`
- `stg_iqvia_csd_meetings_raw`

Rationale:

- Phase 1 policy P-4 says ChannelDynamics, Keywords, and Meetings all remain staging targets.
- Keywords and Meetings are preserved as raw JSON rows because their downstream semantic usage is not normalized in Phase 1.

## Decision 9: UBIST single-month snapshot remains out of Phase 1 schema scope

Status: `VALID`

The UBIST table is designed for the 5-year time-series source. The 2026.02 single-month snapshot is not modeled as a separate staging table.

Rationale:

- Phase 1 policy P-1 selected the archive baseline policy: use the 5-year series and ignore the single-month snapshot for loading.

## Decision 10: Preserve full raw row JSON where layouts vary

Status: `VALID`

`raw_row_json` or `raw_extra_json` is present where raw layouts are wide, duplicated, or not fully normalized in Phase 1:

- `stg_master_drug.raw_row_json`
- `stg_master_market_definition.raw_row_json`
- IQVIA `raw_extra_json`
- CSD `raw_row_json`
- UBIST `raw_extra_json`

Rationale:

- Phase 1 must not infer away unknown columns.
- Later loading and validation phases need raw traceability without revising the schema for every edge column.

## Decision 11: NSA 4Q + 2Q union staging

Status: `VALID`

P-3-A final policy selects a unified NSA staging source using both the 4Q and 2Q raw CSV files.

Policy:

1. Raw inputs are `NSA_IQVIA_2025 4Q.csv` and `NSA_IQVIA_National Sales Audit_2Q 2025_3comb ...csv`.
2. Staging preserves 22 quarterly periods where available: `2020-Q3` through `2025-Q4`.
3. `observations_json` keeps only quarterly metrics. Annual and MAT columns are excluded because they can be derived from quarter data.
4. `price` is preserved:
   - 2Q periods use raw `Price`.
   - 4Q-only periods derive `Price` as `Values LC / Units`.
5. Common-row metadata uses 2Q priority. This resolves raw NHI mismatches by keeping the 2Q value in staging.
6. 4Q-only metadata columns `PACK SIZE`, `PRODUCT AGE`, and `PACK AGE` are not promoted to relational columns.
7. The final `product_key` policy remains `audit_code + mfr_code + product_name + pack_desc + strength + nhi_type + molecule_desc`.
8. Union matching uses a separate key that excludes `NHI TYPE` so mismatched NHI values are resolved by the 2Q-priority policy instead of producing duplicate common rows.

Verification:

- Raw union dry-run produced 68,362 rows: 63,309 common, 3,012 4Q-only, 2,041 2Q-only.
- Price inverse formula `Values LC / Units` matched 925/925 sampled 2Q observations.
- Phase 3a verifier confirmed product_key collision count 0 and sample brand raw-vs-staging sums PASS.

Directive: Keep `etl/iqvia_price_inverse.py` isolated. If IQVIA later supplies raw Price for all quarters, remove the inverse call and preserve raw Price directly without changing the JSON metric name.
