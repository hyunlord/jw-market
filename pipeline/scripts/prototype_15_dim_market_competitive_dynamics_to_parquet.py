"""
prototype_15_dim_market_competitive_dynamics_to_parquet.py
===========================================================
Phase 12 Round 5 dim_market_competitive_dynamics -> Parquet.

Inputs:
- parquet/dim_market_landscape/dim_market_landscape.parquet
- parquet/master_market_definition/master_market_definition.parquet
- parquet/master_drug/master_drug.parquet

Output:
- parquet/dim_market_competitive_dynamics/dim_market_competitive_dynamics.parquet

Policy:
- This table is newly defined by Phase 11 Step C-3 and adjusted by Q-29.
- The grain is one Competitive Dynamics market unit (19 rows).
- cd_005 applies Q-29 option B: [C1D] only, not [C1E].
- cd_008/cd_009 use corrected Phase 12 strategy_008 class_2 values from the
  explicit lookup join.
- All output columns are string dtype.
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as e:
    sys.exit(f"ERROR: {e}\n  pip3 install pyarrow --break-system-packages")


DEFAULT_DIM_MARKET_LANDSCAPE_FILE = Path(
    "parquet/dim_market_landscape/dim_market_landscape.parquet"
)
DEFAULT_MARKET_DEFINITION_FILE = Path(
    "parquet/master_market_definition/master_market_definition.parquet"
)
DEFAULT_MASTER_DRUG_FILE = Path("parquet/master_drug/master_drug.parquet")
DEFAULT_OUTPUT_FILE = Path(
    "parquet/dim_market_competitive_dynamics/dim_market_competitive_dynamics.parquet"
)

EXPECTED_SOURCE_FILE_VERSION = "MI팀_시장분석 AI_시장 분석 Master Version (260422).xlsx"

DIM_MARKET_COMPETITIVE_DYNAMICS_COLUMNS = (
    "competitive_dynamics_id",
    "parent_market_landscape_id",
    "strategic_market_id",
    "sheet_name",
    "data_source_type",
    "product_name_kor",
    "col_in_master_excel",
    "cd_definition_type",
    "cd_filter_expression",
    "cd_filter_status",
    "cd_filter_raw_json",
    "cd_definition_brand_class",
    "cd_brand_count",
    "cd_brand_list_json",
    "target_customer_priority_raw_json",
    "analysis_levels_json",
    "source_file_version",
    "ingested_at",
)

EXPECTED_CD_COUNTS = {
    "cd_001": 116,
    "cd_002": 24,
    "cd_003": 18,
    "cd_004": 10,
    "cd_005": 11,
    "cd_006": 1047,
    "cd_007": 117,
    "cd_008": 20,
    "cd_009": 26,
    "cd_010": 160,
    "cd_011": 140,
    "cd_012": 8,
    "cd_013": 2,
    "cd_014": 26,
    "cd_015": 16,
    "cd_016": 14,
    "cd_017": 4,
    "cd_018": 64,
    "cd_019": 8,
}
EXPECTED_TOTAL_CD_BRAND_COUNT = 1831
EXPECTED_DEFINITION_TYPE_COUNTS = {
    "filter_explicit": 12,
    "ml_equals_cd_exact": 5,
    "ml_equals_cd_by_empty": 1,
    "collapse_pair": 1,
}


CD_SPECS: tuple[dict[str, Any], ...] = (
    {
        "competitive_dynamics_id": "cd_001",
        "strategic_market_id": "strategy_001",
        "product_name_kor": "라베칸/라베칸듀오",
        "col_in_master_excel": "C",
        "column_ids": (3,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "Rabeprazole 단일제 + Rabeprazole/제산제 FDC",
        "cd_filter_expression": "clean(molecule) == 'Rabeprazole'",
        "filter_kind": "molecule_rabeprazole",
    },
    {
        "competitive_dynamics_id": "cd_002",
        "strategic_market_id": "strategy_002",
        "product_name_kor": "제이클",
        "col_in_master_excel": "D",
        "column_ids": (4,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "NON_NHI",
        "cd_filter_expression": "clean(nhi_type) == 'NON-NHI'",
        "filter_kind": "nhi_non_nhi",
    },
    {
        "competitive_dynamics_id": "cd_003",
        "strategic_market_id": "strategy_003",
        "product_name_kor": "가드렛/가드메트",
        "col_in_master_excel": "E",
        "column_ids": (5,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "A10N3 + A10N1",
        "cd_filter_expression": "atc4_code contains A10N3 or A10N1",
        "filter_kind": "atc_a10n3_a10n1",
    },
    {
        "competitive_dynamics_id": "cd_004",
        "strategic_market_id": "strategy_004",
        "product_name_kor": "타발리스",
        "col_in_master_excel": "F",
        "column_ids": (6,),
        "cd_definition_type": "ml_equals_cd_exact",
        "cd_definition_brand_class": "default",
        "cd_filter_expression": "sheet 전체",
        "filter_kind": "sheet_all",
    },
    {
        "competitive_dynamics_id": "cd_005",
        "strategic_market_id": "strategy_005",
        "product_name_kor": "시그마트",
        "col_in_master_excel": "G",
        "column_ids": (7,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "C01D0 -> [C1D] only",
        "cd_filter_expression": "Q-29 option B: atc4_code contains [C1D] only",
        "filter_kind": "sigmart_c1d_only",
        "cd_filter_status": "confirmed_q29_b",
    },
    {
        "competitive_dynamics_id": "cd_006",
        "strategic_market_id": "strategy_006",
        "product_name_kor": "리바로/리바로젯",
        "col_in_master_excel": "H",
        "column_ids": (8,),
        "cd_definition_type": "ml_equals_cd_exact",
        "cd_definition_brand_class": "default",
        "cd_filter_expression": "sheet 전체",
        "filter_kind": "sheet_all",
    },
    {
        "competitive_dynamics_id": "cd_007",
        "strategic_market_id": "strategy_007",
        "product_name_kor": "리바로페노",
        "col_in_master_excel": "I",
        "column_ids": (9,),
        "cd_definition_type": "ml_equals_cd_exact",
        "cd_definition_brand_class": "default",
        "cd_filter_expression": "sheet 전체",
        "filter_kind": "sheet_all",
    },
    {
        "competitive_dynamics_id": "cd_008",
        "strategic_market_id": "strategy_008",
        "product_name_kor": "리바로하이",
        "col_in_master_excel": "J",
        "column_ids": (10,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "Statin/ARB/CCB",
        "cd_filter_expression": "corrected explicit lookup clean(class_2) == 'Statin/ARB/CCB'",
        "filter_kind": "class2_statin_arb_ccb",
    },
    {
        "competitive_dynamics_id": "cd_009",
        "strategic_market_id": "strategy_008",
        "product_name_kor": "리바로브이",
        "col_in_master_excel": "K",
        "column_ids": (11,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "Statin/ARB",
        "cd_filter_expression": "corrected explicit lookup clean(class_2) == 'Statin/ARB'",
        "filter_kind": "class2_statin_arb",
    },
    {
        "competitive_dynamics_id": "cd_010",
        "strategic_market_id": "strategy_009",
        "product_name_kor": "트루패스",
        "col_in_master_excel": "L",
        "column_ids": (12,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "G4C2",
        "cd_filter_expression": "atc4_code contains G4C2",
        "filter_kind": "atc_g4c2",
    },
    {
        "competitive_dynamics_id": "cd_011",
        "strategic_market_id": "strategy_009",
        "product_name_kor": "피나스타/제이다트",
        "col_in_master_excel": "M",
        "column_ids": (13,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "G4C3",
        "cd_filter_expression": "atc4_code contains G4C3",
        "filter_kind": "atc_g4c3",
    },
    {
        "competitive_dynamics_id": "cd_012",
        "strategic_market_id": "strategy_010",
        "product_name_kor": "뉴트로진",
        "col_in_master_excel": "N",
        "column_ids": (14,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "L03A1",
        "cd_filter_expression": "clean(atc4_code) == 'L03A1'",
        "filter_kind": "atc_l03a1",
    },
    {
        "competitive_dynamics_id": "cd_013",
        "strategic_market_id": "strategy_010",
        "product_name_kor": "모빌리아",
        "col_in_master_excel": "O",
        "column_ids": (15,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "L03A9",
        "cd_filter_expression": "clean(atc4_code) == 'L03A9'",
        "filter_kind": "atc_l03a9",
    },
    {
        "competitive_dynamics_id": "cd_014",
        "strategic_market_id": "strategy_011",
        "product_name_kor": "악템라",
        "col_in_master_excel": "P",
        "column_ids": (16,),
        "cd_definition_type": "ml_equals_cd_by_empty",
        "cd_definition_brand_class": "default_sheet_all",
        "cd_filter_expression": "R48-R50 empty -> sheet 전체",
        "filter_kind": "sheet_all",
    },
    {
        "competitive_dynamics_id": "cd_015",
        "strategic_market_id": "strategy_012",
        "product_name_kor": "페린젝트 + 베노훼럼",
        "col_in_master_excel": "Q+R",
        "column_ids": (17, 18),
        "cd_definition_type": "collapse_pair",
        "cd_definition_brand_class": "IV Iron",
        "cd_filter_expression": "clean(atc4_code) == 'B03A1' and clean(dosage_form) == 'IV Iron'",
        "filter_kind": "b03a1_iv_iron",
    },
    {
        "competitive_dynamics_id": "cd_016",
        "strategic_market_id": "strategy_013",
        "product_name_kor": "헴리브라",
        "col_in_master_excel": "S",
        "column_ids": (19,),
        "cd_definition_type": "ml_equals_cd_exact",
        "cd_definition_brand_class": "default",
        "cd_filter_expression": "sheet 전체",
        "filter_kind": "sheet_all",
    },
    {
        "competitive_dynamics_id": "cd_017",
        "strategic_market_id": "strategy_015",
        "product_name_kor": "엔커버",
        "col_in_master_excel": "T",
        "column_ids": (20,),
        "cd_definition_type": "ml_equals_cd_exact",
        "cd_definition_brand_class": "default",
        "cd_filter_expression": "sheet 전체",
        "filter_kind": "sheet_all",
    },
    {
        "competitive_dynamics_id": "cd_018",
        "strategic_market_id": "strategy_014",
        "product_name_kor": "위너프/위너프A+",
        "col_in_master_excel": "U",
        "column_ids": (21,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "3CB & NHI & strength exists",
        "cd_filter_expression": "clean(class) == '3CB' and clean(nhi_type) == 'NHI' and clean(strength) is not null",
        "filter_kind": "winnerf_3cb_nhi_strength",
    },
    {
        "competitive_dynamics_id": "cd_019",
        "strategic_market_id": "strategy_016",
        "product_name_kor": "플라주오피",
        "col_in_master_excel": "V",
        "column_ids": (22,),
        "cd_definition_type": "filter_explicit",
        "cd_definition_brand_class": "Acetated Balanced Crystalloid",
        "cd_filter_expression": "clean(atc4_code) in (K01A1,K01A3) and clean(class) == 'Acetated Balanced Crystalloid'",
        "filter_kind": "plajuopi_acetated",
    },
)


def utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"required parquet not found: {path}")
    return pq.read_table(path).to_pylist()


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def contains_text(value: Any, needle: str) -> bool:
    text = clean_text(value)
    return bool(text and needle in text)


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def excel_column_name(column_id: int) -> str:
    name = ""
    index = column_id
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _source_file_version(rows: list[dict[str, Any]]) -> str:
    versions = {
        unicodedata.normalize("NFC", str(row.get("source_file_version")))
        for row in rows
        if clean_text(row.get("source_file_version")) is not None
    }
    if versions != {EXPECTED_SOURCE_FILE_VERSION}:
        raise ValueError(
            f"source_file_version mismatch: expected={EXPECTED_SOURCE_FILE_VERSION!r}, "
            f"actual={sorted(versions)}"
        )
    return EXPECTED_SOURCE_FILE_VERSION


def raw_column_by_id(market_definition_row: dict[str, Any]) -> dict[int, dict[str, Any]]:
    payload = json.loads(str(market_definition_row["raw_row_json"]))
    return {int(column["column_id"]): column for column in payload.get("columns", [])}


def raw_slots(
    market_definition_row: dict[str, Any],
    column_ids: tuple[int, ...],
    row_ids: tuple[int, ...],
) -> list[dict[str, Any]]:
    columns = raw_column_by_id(market_definition_row)
    slots: list[dict[str, Any]] = []
    for column_id in column_ids:
        column = columns.get(column_id)
        if column is None:
            raise ValueError(
                f"{market_definition_row['strategic_market_id']} raw_row_json missing column_id={column_id}"
            )
        values_by_row = {
            int(item["row_id"]): item
            for item in column.get("values", [])
        }
        for row_id in row_ids:
            item = values_by_row.get(row_id)
            slots.append(
                {
                    "column_id": excel_column_name(column_id),
                    "label": item.get("label") if item else None,
                    "product_name_kor": column.get("product_name"),
                    "row_id": row_id,
                    "value": item.get("value") if item else None,
                }
            )
    return slots


def filter_master_drug_rows(
    spec: dict[str, Any],
    master_drug_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    strategic_market_id = str(spec["strategic_market_id"])
    market_rows = [
        row for row in master_drug_rows
        if str(row.get("strategic_market_id")) == strategic_market_id
    ]
    filter_kind = str(spec["filter_kind"])

    predicates: dict[str, Callable[[dict[str, Any]], bool]] = {
        "sheet_all": lambda row: True,
        "molecule_rabeprazole": lambda row: clean_text(row.get("molecule")) == "Rabeprazole",
        "nhi_non_nhi": lambda row: clean_text(row.get("nhi_type")) == "NON-NHI",
        "atc_a10n3_a10n1": lambda row: contains_text(row.get("atc4_code"), "A10N3")
        or contains_text(row.get("atc4_code"), "A10N1"),
        "sigmart_c1d_only": lambda row: contains_text(row.get("atc4_code"), "C1D"),
        "class2_statin_arb_ccb": lambda row: clean_text(row.get("class_2")) == "Statin/ARB/CCB",
        "class2_statin_arb": lambda row: clean_text(row.get("class_2")) == "Statin/ARB",
        "atc_g4c2": lambda row: contains_text(row.get("atc4_code"), "G4C2"),
        "atc_g4c3": lambda row: contains_text(row.get("atc4_code"), "G4C3"),
        "atc_l03a1": lambda row: clean_text(row.get("atc4_code")) == "L03A1",
        "atc_l03a9": lambda row: clean_text(row.get("atc4_code")) == "L03A9",
        "b03a1_iv_iron": lambda row: clean_text(row.get("atc4_code")) == "B03A1"
        and clean_text(row.get("dosage_form")) == "IV Iron",
        "winnerf_3cb_nhi_strength": lambda row: clean_text(row.get("class")) == "3CB"
        and clean_text(row.get("nhi_type")) == "NHI"
        and clean_text(row.get("strength")) is not None,
        "plajuopi_acetated": lambda row: clean_text(row.get("atc4_code")) in {"K01A1", "K01A3"}
        and clean_text(row.get("class")) == "Acetated Balanced Crystalloid",
    }
    if filter_kind not in predicates:
        raise ValueError(f"unknown filter_kind: {filter_kind}")
    filtered = [row for row in market_rows if predicates[filter_kind](row)]
    filtered.sort(key=lambda row: int(str(row.get("drug_index"))))
    return filtered


def brand_list_payload(filtered_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "row_count": len(filtered_rows),
        "brands": [
            {
                "drug_index": int(str(row.get("drug_index"))),
                "product_name": clean_text(row.get("product_name")),
                "pack": clean_text(row.get("pack_desc")),
                "strength": clean_text(row.get("strength")),
            }
            for row in filtered_rows
        ],
    }


def make_record(
    spec: dict[str, Any],
    dim_market_landscape_by_smid: dict[str, dict[str, Any]],
    market_definition_by_smid: dict[str, dict[str, Any]],
    master_drug_rows: list[dict[str, Any]],
    ingested_at: str,
) -> dict[str, str]:
    strategic_market_id = str(spec["strategic_market_id"])
    landscape_row = dim_market_landscape_by_smid[strategic_market_id]
    market_definition_row = market_definition_by_smid[strategic_market_id]
    filtered_rows = filter_master_drug_rows(spec, master_drug_rows)
    brand_payload = brand_list_payload(filtered_rows)

    return {
        "competitive_dynamics_id": str(spec["competitive_dynamics_id"]),
        "parent_market_landscape_id": str(landscape_row["market_landscape_id"]),
        "strategic_market_id": strategic_market_id,
        "sheet_name": str(landscape_row["sheet_name"]),
        "data_source_type": str(landscape_row["data_source_type"]),
        "product_name_kor": str(spec["product_name_kor"]),
        "col_in_master_excel": str(spec["col_in_master_excel"]),
        "cd_definition_type": str(spec["cd_definition_type"]),
        "cd_filter_expression": str(spec["cd_filter_expression"]),
        "cd_filter_status": str(spec.get("cd_filter_status", "confirmed")),
        "cd_filter_raw_json": dumps_json(
            raw_slots(market_definition_row, tuple(spec["column_ids"]), (48, 49, 50))
        ),
        "cd_definition_brand_class": str(spec["cd_definition_brand_class"]),
        "cd_brand_count": str(brand_payload["row_count"]),
        "cd_brand_list_json": dumps_json(brand_payload),
        "target_customer_priority_raw_json": dumps_json(
            raw_slots(market_definition_row, tuple(spec["column_ids"]), (54, 55, 56, 57))
        ),
        "analysis_levels_json": dumps_json(
            raw_slots(market_definition_row, tuple(spec["column_ids"]), (14, 15, 16, 17, 18, 19))
        ),
        "source_file_version": EXPECTED_SOURCE_FILE_VERSION,
        "ingested_at": ingested_at,
    }


def load_dim_market_competitive_dynamics_records(
    dim_market_landscape_path: Path,
    market_definition_path: Path,
    master_drug_path: Path,
    ingested_at: str | None = None,
) -> list[dict[str, str]]:
    dim_market_landscape_rows = read_parquet_rows(dim_market_landscape_path)
    market_definition_rows = read_parquet_rows(market_definition_path)
    master_drug_rows = read_parquet_rows(master_drug_path)
    _source_file_version(dim_market_landscape_rows)
    _source_file_version(market_definition_rows)
    _source_file_version(master_drug_rows)

    dim_market_landscape_by_smid = {
        str(row["strategic_market_id"]): row for row in dim_market_landscape_rows
    }
    market_definition_by_smid = {
        str(row["strategic_market_id"]): row for row in market_definition_rows
    }
    timestamp = ingested_at or utc_now_text()

    records = [
        make_record(
            spec,
            dim_market_landscape_by_smid,
            market_definition_by_smid,
            master_drug_rows,
            timestamp,
        )
        for spec in CD_SPECS
    ]
    validate_records(records, dim_market_landscape_rows, market_definition_rows, master_drug_rows)
    return records


def validate_records(
    records: list[dict[str, Any]],
    dim_market_landscape_rows: list[dict[str, Any]],
    market_definition_rows: list[dict[str, Any]],
    master_drug_rows: list[dict[str, Any]],
) -> None:
    if len(records) != 19:
        raise ValueError(f"row count must be 19, found={len(records)}")
    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != DIM_MARKET_COMPETITIVE_DYNAMICS_COLUMNS:
            raise ValueError(
                f"row {index} columns mismatch: expected={DIM_MARKET_COMPETITIVE_DYNAMICS_COLUMNS}, "
                f"actual={tuple(record.keys())}"
            )
        for column, value in record.items():
            if value is not None and not isinstance(value, str):
                raise ValueError(f"row {index} column {column} must be string/None, got={type(value)}")

    cd_ids = [record["competitive_dynamics_id"] for record in records]
    expected_cd_ids = [f"cd_{index:03d}" for index in range(1, 20)]
    if cd_ids != expected_cd_ids:
        raise ValueError(f"competitive_dynamics_id sequence mismatch: {cd_ids}")
    if len(set(cd_ids)) != 19:
        raise ValueError("competitive_dynamics_id must be unique")

    landscape_ids = {str(row["market_landscape_id"]) for row in dim_market_landscape_rows}
    market_ids = {str(row["strategic_market_id"]) for row in market_definition_rows}
    for record in records:
        if record["parent_market_landscape_id"] not in landscape_ids:
            raise ValueError(f"missing landscape FK: {record['parent_market_landscape_id']}")
        if record["strategic_market_id"] not in market_ids:
            raise ValueError(f"missing market FK: {record['strategic_market_id']}")

    definition_counts = Counter(record["cd_definition_type"] for record in records)
    if dict(definition_counts) != EXPECTED_DEFINITION_TYPE_COUNTS:
        raise ValueError(
            f"cd_definition_type distribution mismatch: "
            f"expected={EXPECTED_DEFINITION_TYPE_COUNTS}, actual={dict(definition_counts)}"
        )

    cd_counts = {
        str(record["competitive_dynamics_id"]): int(str(record["cd_brand_count"]))
        for record in records
    }
    if cd_counts != EXPECTED_CD_COUNTS:
        raise ValueError(f"cd_brand_count mismatch: expected={EXPECTED_CD_COUNTS}, actual={cd_counts}")
    if sum(cd_counts.values()) != EXPECTED_TOTAL_CD_BRAND_COUNT:
        raise ValueError(f"total cd_brand_count mismatch: {sum(cd_counts.values())}")

    for record in records:
        cd_id = str(record["competitive_dynamics_id"])
        for json_column in (
            "cd_filter_raw_json",
            "cd_brand_list_json",
            "target_customer_priority_raw_json",
            "analysis_levels_json",
        ):
            try:
                parsed = json.loads(str(record[json_column]))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{cd_id} {json_column} invalid JSON: {exc}") from exc
            if json_column != "cd_brand_list_json" and not isinstance(parsed, list):
                raise ValueError(f"{cd_id} {json_column} must be a JSON array")

        brand_payload = json.loads(str(record["cd_brand_list_json"]))
        brand_count = int(str(record["cd_brand_count"]))
        if brand_payload.get("row_count") != brand_count:
            raise ValueError(f"{cd_id} cd_brand_list_json row_count mismatch")
        brands = brand_payload.get("brands")
        if not isinstance(brands, list) or len(brands) != brand_count:
            raise ValueError(f"{cd_id} cd_brand_list_json brands length mismatch")
        for brand in brands:
            if tuple(brand.keys()) != ("drug_index", "pack", "product_name", "strength"):
                raise ValueError(f"{cd_id} brand shape mismatch: {brand}")
            if not isinstance(brand["drug_index"], int):
                raise ValueError(f"{cd_id} drug_index must be int inside JSON")

    strategy_008_class2_counts = Counter(
        clean_text(row.get("class_2"))
        for row in master_drug_rows
        if str(row.get("strategic_market_id")) == "strategy_008"
        and clean_text(row.get("class_2")) is not None
    )
    if sum(strategy_008_class2_counts.values()) != 88:
        raise ValueError(f"strategy_008 class_2 non-null count must be 88: {strategy_008_class2_counts}")
    other_strategy_008 = sum(strategy_008_class2_counts.values()) - (
        EXPECTED_CD_COUNTS["cd_008"] + EXPECTED_CD_COUNTS["cd_009"]
    )
    if other_strategy_008 != 42:
        raise ValueError(f"strategy_008 non-CD class_2 count must be 42, found={other_strategy_008}")


def write_parquet(records: list[dict[str, Any]], output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    schema = pa.schema(
        [pa.field(column, pa.string()) for column in DIM_MARKET_COMPETITIVE_DYNAMICS_COLUMNS]
    )
    rows = [
        {
            column: None if record.get(column) is None else str(record.get(column))
            for column in DIM_MARKET_COMPETITIVE_DYNAMICS_COLUMNS
        }
        for record in records
    ]
    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(table, output_file, compression="zstd", compression_level=3)


def _count_by(records: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value = str(record.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load Phase 12 dim_market_competitive_dynamics parquet."
    )
    parser.add_argument(
        "--dim-market-landscape",
        type=Path,
        default=DEFAULT_DIM_MARKET_LANDSCAPE_FILE,
    )
    parser.add_argument("--market-definition", type=Path, default=DEFAULT_MARKET_DEFINITION_FILE)
    parser.add_argument("--master-drug", type=Path, default=DEFAULT_MASTER_DRUG_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    records = load_dim_market_competitive_dynamics_records(
        args.dim_market_landscape,
        args.market_definition,
        args.master_drug,
    )
    write_parquet(records, args.output)

    print("prototype Phase 12 Round 5 dim_market_competitive_dynamics -> Parquet")
    print(f"rows={len(records)}")
    print(f"columns={len(DIM_MARKET_COMPETITIVE_DYNAMICS_COLUMNS)}")
    print(f"output={args.output}")
    print(f"source_file_version={records[0]['source_file_version']}")
    print(f"ingested_at={records[0]['ingested_at']}")
    print("cd_definition_type_distribution:")
    for definition_type, count in sorted(_count_by(records, "cd_definition_type").items()):
        print(f"  {definition_type}: {count}")
    print("cd_brand_count:")
    for record in records:
        print(f"  {record['competitive_dynamics_id']}: {record['cd_brand_count']}")
    print(f"cd_brand_count_total={sum(int(record['cd_brand_count']) for record in records)}")
    print("validate_records: PASS")


if __name__ == "__main__":
    main()
