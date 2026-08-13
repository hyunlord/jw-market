from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any, Callable

from pipeline.etl.io.catalog._lib.common import (
    clean_text,
    dumps_compact_json,
    read_parquet_rows,
    utc_now_text,
)
from pipeline.etl.io.catalog.dim.market_competitive_dynamics_schema import EXPECTED_SOURCE_FILE_VERSION
from pipeline.etl.io.catalog.dim.market_competitive_dynamics_specs import CD_SPECS
from pipeline.etl.io.catalog.dim.market_competitive_dynamics_validation import validate_records

def contains_text(value: Any, needle: str) -> bool:
    text = clean_text(value)
    return bool(text and needle in text)


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
    if versions != {unicodedata.normalize("NFC", EXPECTED_SOURCE_FILE_VERSION)}:
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

    if filter_kind == "master_atc4":
        allowed = {str(value) for value in spec["filter_values"]}
        filtered = [
            row
            for row in market_rows
            if clean_text(row.get("atc4_code")) in allowed
        ]
        filtered.sort(key=lambda row: int(str(row.get("drug_index"))))
        return filtered

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
        "cd_filter_raw_json": dumps_compact_json(
            raw_slots(market_definition_row, tuple(spec["column_ids"]), (48, 49, 50))
        ),
        "cd_definition_brand_class": str(spec["cd_definition_brand_class"]),
        "cd_brand_count": str(brand_payload["row_count"]),
        "cd_brand_list_json": dumps_compact_json(brand_payload),
        "target_customer_priority_raw_json": dumps_compact_json(
            raw_slots(market_definition_row, tuple(spec["column_ids"]), (54, 55, 56, 57))
        ),
        "analysis_levels_json": dumps_compact_json(
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
