from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any

from pipeline.etl.io.catalog._lib.common import (
    clean_text,
    dumps_compact_json,
    read_parquet_rows,
    utc_now_text,
)
from pipeline.etl.io.catalog.dim.market_landscape_schema import (
    DEFAULT_SHEET_ALL_MARKETS,
    EXPECTED_MARKET_IDS,
    EXPECTED_SOURCE_FILE_VERSION,
)
from pipeline.etl.io.catalog.dim.market_landscape_validation import validate_records

def parse_json_text(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, float):
        return fallback
    return json.loads(str(value))


def join_unique(values: list[Any]) -> str | None:
    seen: list[str] = []
    for value in values:
        text = clean_text(value)
        if text and text not in seen:
            seen.append(text)
    if not seen:
        return None
    return " / ".join(seen)


def raw_values_by_row(raw_row_json: str) -> dict[int, list[Any]]:
    payload = json.loads(raw_row_json)
    values_by_row: dict[int, list[Any]] = {}
    for column in payload.get("columns", []):
        for item in column.get("values", []):
            row_id = int(item["row_id"])
            values_by_row.setdefault(row_id, []).append(item.get("value"))
    return values_by_row


def raw_row_value(raw_row_json: str, row_id: int) -> str | None:
    return join_unique(raw_values_by_row(raw_row_json).get(row_id, []))


def metric_json_from_analysis_levels(analysis_levels_json: Any) -> str:
    analysis_levels = parse_json_text(analysis_levels_json, {})
    return dumps_compact_json(analysis_levels.get("Metrics", []))


def master_drug_brand_payload(
    strategic_market_id: str,
    master_drug_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    market_rows = [
        row for row in master_drug_rows
        if str(row.get("strategic_market_id")) == strategic_market_id
    ]
    market_rows.sort(key=lambda row: int(str(row.get("drug_index"))))
    return {
        "row_count": len(market_rows),
        "brands": [
            {
                "drug_index": int(str(row.get("drug_index"))),
                "product_name": clean_text(row.get("product_name")),
            }
            for row in market_rows
        ],
    }


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


def make_record(
    ordinal: int,
    market_definition_row: dict[str, Any],
    master_drug_rows: list[dict[str, Any]],
    ingested_at: str,
) -> dict[str, str | None]:
    strategic_market_id = str(market_definition_row["strategic_market_id"])
    raw_row_json = str(market_definition_row["raw_row_json"])
    brand_payload = master_drug_brand_payload(strategic_market_id, master_drug_rows)
    ml_definition_type = (
        "default_sheet_all"
        if strategic_market_id in DEFAULT_SHEET_ALL_MARKETS
        else "atc_codes_explicit"
    )

    return {
        "market_landscape_id": f"ml_{ordinal:03d}",
        "strategic_market_id": strategic_market_id,
        "sheet_name": clean_text(market_definition_row.get("market_name")),
        "product_name_kor_in_sheet": raw_row_value(raw_row_json, 6),
        "atc4_code": raw_row_value(raw_row_json, 7),
        "atc4_desc": raw_row_value(raw_row_json, 8),
        "nhi_type": raw_row_value(raw_row_json, 9),
        "data_source_type": clean_text(market_definition_row.get("source_type")),
        "analysis_value_raw": raw_row_value(raw_row_json, 11),
        "mkt_team_jwp_mkt": raw_row_value(raw_row_json, 5),
        "ml_definition_type": ml_definition_type,
        "ml_atc_codes_json": dumps_compact_json(
            parse_json_text(market_definition_row.get("full_market_atc4_codes_json"), [])
        ),
        "ml_brand_count": str(brand_payload["row_count"]),
        "ml_brand_list_json": dumps_compact_json(brand_payload),
        "analysis_metrics_json": metric_json_from_analysis_levels(
            market_definition_row.get("analysis_levels_json")
        ),
        "source_file_version": clean_text(market_definition_row.get("source_file_version")),
        "ingested_at": ingested_at,
    }


def load_dim_market_landscape_records(
    market_definition_path: Path,
    master_drug_path: Path,
    ingested_at: str | None = None,
) -> list[dict[str, str | None]]:
    market_definition_rows = read_parquet_rows(market_definition_path)
    master_drug_rows = read_parquet_rows(master_drug_path)
    _source_file_version(market_definition_rows)
    _source_file_version(master_drug_rows)

    market_definition_by_id = {
        str(row.get("strategic_market_id")): row for row in market_definition_rows
    }
    expected_ids = set(EXPECTED_MARKET_IDS)
    actual_ids = set(market_definition_by_id)
    if actual_ids != expected_ids:
        raise ValueError(
            f"market_definition strategic_market_id mismatch: "
            f"missing={sorted(expected_ids - actual_ids)}, extra={sorted(actual_ids - expected_ids)}"
        )

    timestamp = ingested_at or utc_now_text()
    records = [
        make_record(
            ordinal=index,
            market_definition_row=market_definition_by_id[strategic_market_id],
            master_drug_rows=master_drug_rows,
            ingested_at=timestamp,
        )
        for index, strategic_market_id in enumerate(EXPECTED_MARKET_IDS, start=1)
    ]
    validate_records(records, market_definition_rows, master_drug_rows)
    return records
