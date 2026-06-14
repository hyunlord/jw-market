# Master Column Mapping Catalog — Phase 1

Generated: 2026-05-09

## Scope

This catalog defines the `column_metadata_json` intent for the 16 market sheets in the Master workbook. It is a schema-design reference only; no rows are staged in Phase 1.

Source references:

- `docs/reference/POLICIES_FOR_PHASE_1.md`
- `docs/reference/raw_master_xlsx_per_sheet/sheet_05_*.md` through `sheet_20_*.md`
- `docs/v26_archive_for_reference/master/MASTER_16_SHEETS_COLUMN_DEFINITIONS_V3.md`

## Metadata Shape

```json
{
  "standard_column": {
    "source_column": "raw Master column",
    "type": "raw | manual_overlay | manual_added | raw_supplementary | manual_supplementary",
    "overlay_target": "raw column to override in strategic view, or null",
    "override_priority": "master_first or null"
  }
}
```

Type definitions:

- `raw`: Master column mirrors a raw source column.
- `manual_overlay`: Master value may override the raw value in a later Strategic View.
- `manual_added`: Master-only classification or analysis column.
- `raw_supplementary`: raw-adjacent detail preserved in JSON.
- `manual_supplementary`: manual detail preserved in JSON.

## 16 Market Sheets

### strategy_001 — sheet_05 — 라베칸 라베칸듀오

```json
{
  "atc4_code": {"source_column": "ATC", "type": "raw", "overlay_target": null, "override_priority": null},
  "molecule": {"source_column": "성분 Recode", "type": "manual_overlay", "overlay_target": "성분", "override_priority": "master_first"},
  "seller": {"source_column": "판매사", "type": "raw", "overlay_target": null, "override_priority": null},
  "product_name": {"source_column": "제품", "type": "raw", "overlay_target": null, "override_priority": null},
  "manufacturer": {"source_column": "제조사", "type": "raw", "overlay_target": null, "override_priority": null},
  "class": {"source_column": "Class Recode", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "funnel": {"source_column": "Funnel", "type": "manual_added", "overlay_target": null, "override_priority": null}
}
```

### strategy_002 — sheet_06 — 제이클

```json
{
  "atc4_code": {"source_column": "ATC 4 CODE", "type": "raw", "overlay_target": null, "override_priority": null},
  "atc4_desc": {"source_column": "ATC 4 DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "molecule": {"source_column": "MOLECULE DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "product_name": {"source_column": "PRODUCT NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "manufacturer": {"source_column": "MFR NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "nhi_type": {"source_column": "NHI TYPE", "type": "manual_overlay", "overlay_target": "NHI TYPE", "override_priority": "master_first"},
  "dosage_form": {"source_column": "Recode 제형", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "class": {"source_column": "Recode Class(성분)", "type": "manual_added", "overlay_target": null, "override_priority": null}
}
```

### strategy_003 — sheet_07 — 가드렛 가드메트

```json
{
  "atc4_code": {"source_column": "ATC 4 CODE", "type": "raw", "overlay_target": null, "override_priority": null},
  "atc4_desc": {"source_column": "ATC 4 DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "molecule": {"source_column": "MOLECULE DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "class": {"source_column": "Class Recode", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "dosage_form": {"source_column": "제형 Recode", "type": "manual_added", "overlay_target": null, "override_priority": null}
}
```

### strategy_004 — sheet_08 — 타발리스

```json
{
  "atc4_code": {"source_column": "ATC 4 CODE", "type": "raw", "overlay_target": null, "override_priority": null},
  "atc4_desc": {"source_column": "ATC 4 DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "molecule": {"source_column": "MOLECULE DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "product_name": {"source_column": "PRODUCT NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "manufacturer": {"source_column": "MFR NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "pack_desc": {"source_column": "PACK DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "class": {"source_column": "Class Recode", "type": "manual_added", "overlay_target": null, "override_priority": null}
}
```

### strategy_005 — sheet_09 — 시그마트

```json
{
  "atc4_code": {"source_column": "ATC", "type": "manual_supplementary", "overlay_target": null, "override_priority": null},
  "molecule": {"source_column": "Molecule Recode", "type": "manual_overlay", "overlay_target": "성분", "override_priority": "master_first"},
  "seller": {"source_column": "판매사", "type": "manual_supplementary", "overlay_target": null, "override_priority": null},
  "product_name": {"source_column": "제품", "type": "raw", "overlay_target": null, "override_priority": null},
  "manufacturer": {"source_column": "제조사", "type": "raw", "overlay_target": null, "override_priority": null},
  "class": {"source_column": "Class Recode", "type": "manual_added", "overlay_target": null, "override_priority": null}
}
```

### strategy_006 — sheet_10 — 리바로 리바로젯

```json
{
  "atc4_code": {"source_column": "ATC", "type": "raw", "overlay_target": null, "override_priority": null},
  "molecule": {"source_column": "Molecule", "type": "raw", "overlay_target": null, "override_priority": null},
  "seller": {"source_column": "판매사", "type": "raw", "overlay_target": null, "override_priority": null},
  "product_name": {"source_column": "제품", "type": "raw", "overlay_target": null, "override_priority": null},
  "manufacturer": {"source_column": "제조사", "type": "raw", "overlay_target": null, "override_priority": null},
  "class": {"source_column": "Class", "type": "raw", "overlay_target": null, "override_priority": null},
  "formulation": {"source_column": "성분용량", "type": "raw", "overlay_target": null, "override_priority": null},
  "strength": {"source_column": "Strength", "type": "raw", "overlay_target": null, "override_priority": null},
  "ox_gx": {"source_column": "Ox/Gx", "type": "raw", "overlay_target": null, "override_priority": null},
  "drug_extra_json.molecule_eng": {"source_column": "성분", "type": "raw_supplementary", "overlay_target": null, "override_priority": null}
}
```

### strategy_007 — sheet_11 — 리바로페노

```json
{
  "atc4_code": {"source_column": "ATC", "type": "raw", "overlay_target": null, "override_priority": null},
  "molecule": {"source_column": "성분", "type": "raw", "overlay_target": null, "override_priority": null},
  "seller": {"source_column": "판매사", "type": "raw", "overlay_target": null, "override_priority": null},
  "product_name": {"source_column": "제품", "type": "raw", "overlay_target": null, "override_priority": null},
  "manufacturer": {"source_column": "제조사", "type": "raw", "overlay_target": null, "override_priority": null},
  "formulation": {"source_column": "성분용량", "type": "raw", "overlay_target": null, "override_priority": null},
  "strength": {"source_column": "성분용량", "type": "raw", "overlay_target": null, "override_priority": null},
  "class": {"source_column": "Class", "type": "raw", "overlay_target": null, "override_priority": null},
  "drug_extra_json.class_raw": {"source_column": "Class", "type": "raw_supplementary", "overlay_target": null, "override_priority": null},
  "drug_extra_json.molecule_eng": {"source_column": "Molecule", "type": "raw_supplementary", "overlay_target": null, "override_priority": null}
}
```

### strategy_008 — sheet_12 — 리바로하이 리바로브이

```json
{
  "atc4_code": {"source_column": "ATC", "type": "raw", "overlay_target": null, "override_priority": null},
  "molecule": {"source_column": "성분 Recode", "type": "manual_overlay", "overlay_target": "성분", "override_priority": "master_first"},
  "seller": {"source_column": "판매사", "type": "raw", "overlay_target": null, "override_priority": null},
  "product_name": {"source_column": "제품", "type": "raw", "overlay_target": null, "override_priority": null},
  "manufacturer": {"source_column": "제조사", "type": "raw", "overlay_target": null, "override_priority": null},
  "strength": {"source_column": "Strength", "type": "raw", "overlay_target": null, "override_priority": null},
  "molecule_disease_definition": {"source_column": "질환 정의 Recode", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "composition_type": {"source_column": "단일/복합 Recode", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "class": {"source_column": "Class Recode", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "class_2": {"source_column": "Class Recode 분류2", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "drug_extra_json.ox_gx": {"source_column": "Ox/Gx", "type": "raw_supplementary", "overlay_target": null, "override_priority": null}
}
```

### strategy_009 — sheet_13 — 트루패스 피나스타 제이다트

```json
{
  "atc4_code": {"source_column": "ATC", "type": "raw", "overlay_target": null, "override_priority": null},
  "molecule": {"source_column": "성분", "type": "raw", "overlay_target": null, "override_priority": null},
  "seller": {"source_column": "판매사", "type": "raw", "overlay_target": null, "override_priority": null},
  "product_name": {"source_column": "제품", "type": "raw", "overlay_target": null, "override_priority": null},
  "manufacturer": {"source_column": "제조사", "type": "raw", "overlay_target": null, "override_priority": null},
  "class": {"source_column": "Class", "type": "raw", "overlay_target": null, "override_priority": null},
  "drug_extra_json.class_raw": {"source_column": "Class", "type": "raw_supplementary", "overlay_target": null, "override_priority": null},
  "drug_extra_json.molecule_eng": {"source_column": "Molecule", "type": "raw_supplementary", "overlay_target": null, "override_priority": null}
}
```

### strategy_010 — sheet_14 — 뉴트로진 모빌리아

```json
{
  "atc4_code": {"source_column": "ATC 4 CODE", "type": "raw", "overlay_target": null, "override_priority": null},
  "atc4_desc": {"source_column": "ATC 4 DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "molecule": {"source_column": "MOLECULE DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "nhi_type": {"source_column": "NHI TYPE", "type": "manual_overlay", "overlay_target": "NHI TYPE", "override_priority": "master_first"},
  "manufacturer": {"source_column": "MFR NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "product_name": {"source_column": "PRODUCT NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "class": {"source_column": "Class", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "drug_extra_json.generation": {"source_column": "Generation", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "drug_extra_json.ta": {"source_column": "TA", "type": "manual_added", "overlay_target": null, "override_priority": null}
}
```

### strategy_011 — sheet_15 — 악템라

```json
{
  "atc4_code": {"source_column": "ATC 4 CODE", "type": "raw", "overlay_target": null, "override_priority": null},
  "atc4_desc": {"source_column": "ATC 4 DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "molecule": {"source_column": "MOLECULE DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "product_name": {"source_column": "PRODUCT NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "manufacturer": {"source_column": "MFR NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "class": {"source_column": "Class Recode 1", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "class_2": {"source_column": "Class Recode 2", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "drug_extra_json.ox_gx_biosimilar": {"source_column": "Ox/Gx(바이오시밀러)", "type": "manual_added", "overlay_target": null, "override_priority": null}
}
```

Brand grouping from `Remark` is stored in `stg_master_brand_consolidation`, not embedded in each row.

### strategy_012 — sheet_16 — 페린젝트 베노훼럼

```json
{
  "atc4_code": {"source_column": "ATC 4 CODE", "type": "raw", "overlay_target": null, "override_priority": null},
  "atc4_desc": {"source_column": "ATC 4 DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "molecule": {"source_column": "MOLECULE DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "nhi_type": {"source_column": "NHI TYPE", "type": "manual_overlay", "overlay_target": "NHI TYPE", "override_priority": "master_first"},
  "manufacturer": {"source_column": "MFR NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "product_name": {"source_column": "PRODUCT NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "pack_desc": {"source_column": "PACK DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "class": {"source_column": "Dosage Form", "type": "raw", "overlay_target": null, "override_priority": null},
  "dosage_form": {"source_column": "Dosage Form", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "strength": {"source_column": "Strength", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "drug_extra_json.molecule_eng": {"source_column": "Molecule", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "drug_extra_json.fe_content_per_ml": {"source_column": "1ml 당 Fe 함량", "type": "manual_added", "overlay_target": null, "override_priority": null}
}
```

### strategy_013 — sheet_17 — 헴리브라

```json
{
  "atc4_code": {"source_column": "ATC 4 CODE", "type": "raw", "overlay_target": null, "override_priority": null},
  "atc4_desc": {"source_column": "ATC 4 DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "molecule": {"source_column": "MOLECULE DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "nhi_type": {"source_column": "NHI TYPE", "type": "manual_overlay", "overlay_target": "NHI TYPE", "override_priority": "master_first"},
  "manufacturer": {"source_column": "MFR NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "product_name": {"source_column": "PRODUCT NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "class": {"source_column": "Class", "type": "manual_added", "overlay_target": null, "override_priority": null}
}
```

### strategy_014 — sheet_18 — 위너프 위너프A+

```json
{
  "atc4_code": {"source_column": "ATC 4 CODE", "type": "raw", "overlay_target": null, "override_priority": null},
  "atc4_desc": {"source_column": "ATC 4 DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "molecule": {"source_column": "MOLECULE DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "nhi_type": {"source_column": "NHI TYPE", "type": "manual_overlay", "overlay_target": "NHI TYPE", "override_priority": "master_first"},
  "manufacturer": {"source_column": "MFR NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "product_name": {"source_column": "PRODUCT NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "pack_desc": {"source_column": "PACK DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "strength": {"source_column": "규격 Recode", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "strength_raw": {"source_column": "Strength", "type": "raw", "overlay_target": null, "override_priority": null},
  "strength_raw_2": {"source_column": "Strength2", "type": "raw", "overlay_target": null, "override_priority": null},
  "class": {"source_column": "Class", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "dosage_form": {"source_column": "투여 경로", "type": "raw", "overlay_target": null, "override_priority": null},
  "drug_extra_json.fish_oil_yn": {"source_column": "Fish oil 여부", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "drug_extra_json.administration_route": {"source_column": "투여 경로", "type": "manual_added", "overlay_target": null, "override_priority": null},
  "drug_extra_json.product_pack": {"source_column": "PRODUCT NAME KORPACK DESC", "type": "manual_added", "overlay_target": null, "override_priority": null}
}
```

### strategy_015 — sheet_19 — 엔커버

```json
{
  "atc4_code": {"source_column": "ATC 4 CODE", "type": "raw", "overlay_target": null, "override_priority": null},
  "atc4_desc": {"source_column": "ATC 4 DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "molecule": {"source_column": "MOLECULE DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "nhi_type": {"source_column": "NHI TYPE", "type": "manual_overlay", "overlay_target": "NHI TYPE", "override_priority": "master_first"},
  "manufacturer": {"source_column": "MFR NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "product_name": {"source_column": "PRODUCT NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "pack_desc": {"source_column": "PACK DESC", "type": "raw", "overlay_target": null, "override_priority": null}
}
```

### strategy_016 — sheet_20 — 플라주오피

```json
{
  "atc4_code": {"source_column": "ATC 4 CODE", "type": "raw", "overlay_target": null, "override_priority": null},
  "atc4_desc": {"source_column": "ATC 4 DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "molecule": {"source_column": "MOLECULE DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "nhi_type": {"source_column": "NHI TYPE", "type": "manual_overlay", "overlay_target": "NHI TYPE", "override_priority": "master_first"},
  "manufacturer": {"source_column": "MFR NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "product_name": {"source_column": "PRODUCT NAME KOR", "type": "raw", "overlay_target": null, "override_priority": null},
  "strength": {"source_column": "PACK DESC", "type": "raw", "overlay_target": null, "override_priority": null},
  "class": {"source_column": "Class", "type": "manual_added", "overlay_target": null, "override_priority": null}
}
```

## Global Notes

- Exclusion-marked rows are a Phase 2 Master staging concern. Phase 1 only reserves fields and reference metadata.
- `manual_overlay` means staging preserves both raw data and Master metadata; the overlay is applied only in a later Strategic View.
- Sheet-specific details not promoted to standard columns are preserved through `drug_extra_json` and/or `raw_row_json`.
