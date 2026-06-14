from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.etl.io.catalog.cd_market_schema import (
    CD_MARKET_COLUMNS,
    CD_SPECIFIC_ROWS_TO_VALIDATE,
    CD_SPECS,
    COLLAPSE_PAIR_CD_ID,
    DEFAULT_OUTPUT_FILE,
    EXPECTED_SOURCE_FILE_VERSION,
)
from pipeline.etl.io.catalog.cd_market_validation import validate_records
from pipeline.etl.io.catalog.market_catalog_text import (
    clean_market_text as clean_text,
    read_parquet_rows,
    source_file_version as _source_file_version,
    utc_now_datetime,
)


def source_file_version(rows: list[dict[str, Any]], label: str) -> str:
    return _source_file_version(
        rows,
        expected=EXPECTED_SOURCE_FILE_VERSION,
        label=f"{label} source_file_version",
        cleaner=clean_text,
    )


def raw_column_by_id(market_definition_row: dict[str, Any]) -> dict[int, dict[str, Any]]:
    payload = json.loads(str(market_definition_row["raw_row_json"]))
    return {int(column["column_id"]): column for column in payload.get("columns", [])}


def raw_value_for(
    market_definition_row: dict[str, Any],
    column_id: int,
    row_id: int,
) -> str | None:
    column = raw_column_by_id(market_definition_row).get(column_id)
    if column is None:
        raise ValueError(
            f"{market_definition_row['strategic_market_id']} missing raw column_id={column_id}"
        )
    for item in column.get("values", []):
        if int(item.get("row_id")) == row_id:
            return clean_text(item.get("value"))
    return None


def cd_specific_value(
    spec: dict[str, Any],
    market_definition_row: dict[str, Any],
    row_id: int,
) -> str | None:
    values = [
        raw_value_for(market_definition_row, int(column_id), row_id)
        for column_id in tuple(spec["column_ids"])
    ]
    non_empty = [value for value in values if value is not None]
    if not non_empty:
        return None
    unique_values = []
    for value in non_empty:
        if value not in unique_values:
            unique_values.append(value)
    if spec["cd_id"] == COLLAPSE_PAIR_CD_ID and len(unique_values) > 1:
        raise ValueError(
            f"{COLLAPSE_PAIR_CD_ID} collapse pair raw mismatch at row {row_id}: {values}"
        )
    return unique_values[0]


def target_iqvia_value(
    spec: dict[str, Any],
    market_definition_row: dict[str, Any],
    ml_row: dict[str, Any],
    target_index: int,
) -> str | None:
    return clean_text(ml_row.get(f"target_iqvia_{target_index}"))


def target_ubist_value(
    spec: dict[str, Any],
    market_definition_row: dict[str, Any],
    ml_row: dict[str, Any],
    target_index: int,
) -> str | None:
    return clean_text(ml_row.get(f"target_ubist_{target_index}"))


def make_record(
    spec: dict[str, Any],
    ml_by_id: dict[str, dict[str, Any]],
    market_definition_by_smid: dict[str, dict[str, Any]],
    ingested_at: datetime,
) -> dict[str, Any]:
    ml_row = ml_by_id[str(spec["ml_id"])]
    market_definition_row = market_definition_by_smid[str(spec["strategic_market_id"])]

    record: dict[str, Any] = {
        "cd_id": str(spec["cd_id"]),
        "name": str(spec["name"]),
        "ml_id": str(spec["ml_id"]),
        "cd_filter_id": str(spec["cd_filter_id"]),
        "data_source": str(ml_row["data_source"]),
        "source_file_version": EXPECTED_SOURCE_FILE_VERSION,
        "ingested_at": ingested_at,
    }
    for column in (
        "analyze_class",
        "analyze_molecule",
        "analyze_dosage_form",
        "analyze_strength_pack",
        "analyze_nhi_type",
        "analyze_ox_gx",
        "analyze_fish_oil",
    ):
        record[column] = bool(ml_row[column])
    for index in range(1, 4):
        record[f"target_iqvia_{index}"] = target_iqvia_value(
            spec, market_definition_row, ml_row, index
        )
    for index in range(1, 5):
        record[f"target_ubist_{index}"] = target_ubist_value(
            spec, market_definition_row, ml_row, index
        )
    return {column: record.get(column) for column in CD_MARKET_COLUMNS}


def validate_collapse_pair_raw(
    spec: dict[str, Any],
    market_definition_row: dict[str, Any],
) -> None:
    if spec["cd_id"] != COLLAPSE_PAIR_CD_ID:
        return
    for row_id in CD_SPECIFIC_ROWS_TO_VALIDATE:
        cd_specific_value(spec, market_definition_row, row_id)


def load_cd_market_records(
    ml_market_path: Path,
    cd_filter_path: Path,
    market_definition_path: Path,
    existing_path: Path = DEFAULT_OUTPUT_FILE,
    ingested_at: datetime | None = None,
) -> list[dict[str, Any]]:
    ml_rows = read_parquet_rows(ml_market_path)
    cd_filter_rows = read_parquet_rows(cd_filter_path)
    if not market_definition_path.exists():
        if not existing_path.exists():
            raise FileNotFoundError(
                "Phase 14 source market_definition parquet is missing and existing "
                f"cd_market fallback was not found: {existing_path}"
            )
        return load_existing_cd_market_records(
            existing_path=existing_path,
            ml_rows=ml_rows,
            cd_filter_rows=cd_filter_rows,
            ingested_at=ingested_at,
        )

    market_definition_rows = read_parquet_rows(market_definition_path)
    source_file_version(ml_rows, "ml_market")
    source_file_version(cd_filter_rows, "cd_filter")
    source_file_version(market_definition_rows, "master_market_definition")

    ml_by_id = {str(row["ml_id"]): row for row in ml_rows}
    market_definition_by_smid = {
        str(row["strategic_market_id"]): row for row in market_definition_rows
    }

    for spec in CD_SPECS:
        validate_collapse_pair_raw(
            spec,
            market_definition_by_smid[str(spec["strategic_market_id"])],
        )

    timestamp = ingested_at or utc_now_datetime()
    records = [
        make_record(spec, ml_by_id, market_definition_by_smid, timestamp)
        for spec in CD_SPECS
    ]
    validate_records(records, ml_rows, cd_filter_rows)
    return records


def load_existing_cd_market_records(
    existing_path: Path,
    ml_rows: list[dict[str, Any]],
    cd_filter_rows: list[dict[str, Any]],
    ingested_at: datetime | None = None,
) -> list[dict[str, Any]]:
    rows = read_parquet_rows(existing_path)
    source_file_version(ml_rows, "ml_market")
    source_file_version(cd_filter_rows, "cd_filter")
    source_file_version(rows, "cd_market")
    ml_by_id = {str(row["ml_id"]): row for row in ml_rows}
    timestamp = ingested_at or utc_now_datetime()
    records: list[dict[str, Any]] = []
    for row in rows:
        record = {column: row.get(column) for column in CD_MARKET_COLUMNS}
        ml_row = ml_by_id[str(record["ml_id"])]
        record["data_source"] = str(ml_row["data_source"])
        for column in (
            "analyze_class",
            "analyze_molecule",
            "analyze_dosage_form",
            "analyze_strength_pack",
            "analyze_nhi_type",
            "analyze_ox_gx",
            "analyze_fish_oil",
        ):
            record[column] = bool(ml_row[column])
        for index in range(1, 4):
            record[f"target_iqvia_{index}"] = clean_text(
                ml_row.get(f"target_iqvia_{index}")
            )
        for index in range(1, 5):
            record[f"target_ubist_{index}"] = clean_text(
                ml_row.get(f"target_ubist_{index}")
            )
        record["source_file_version"] = EXPECTED_SOURCE_FILE_VERSION
        record["ingested_at"] = timestamp
        records.append({column: record.get(column) for column in CD_MARKET_COLUMNS})
    validate_records(records, ml_rows, cd_filter_rows)
    return records
