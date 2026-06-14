from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.etl.io.catalog.market_catalog_text import (
    clean_text,
    parse_json_text,
    read_parquet_rows,
    source_file_version,
    utc_now_datetime,
)
from pipeline.etl.io.catalog.ml_market_schema import (
    ANALYZE_COLUMNS,
    ANALYZE_MATRIX,
    AUDIT_CODES,
    DEFAULT_OUTPUT_FILE,
    EXPECTED_MARKET_IDS,
    EXPECTED_SOURCE_FILE_VERSION,
    ML_MARKET_COLUMNS,
    UBIST_TARGET_PATTERN,
)
from pipeline.etl.io.catalog.ml_market_validation import validate_records

def normalize_data_source(value: Any, strategic_market_id: str) -> str:
    text = clean_text(value)
    if text is None:
        raise ValueError(f"{strategic_market_id} source_type is empty")
    lowered = text.lower()
    if lowered not in {"iqvia", "ubist", "both"}:
        raise ValueError(f"{strategic_market_id} invalid source_type={text!r}")
    return lowered


def normalize_market_name(value: Any) -> str:
    text = clean_text(value)
    if text is None:
        raise ValueError("market_name is empty")
    return text.replace("위너프A+", "위너프에이플러스")


def analyze_values_for_ml(ml_id: str) -> dict[str, bool]:
    matrix = ANALYZE_MATRIX.get(ml_id)
    if matrix is None:
        raise ValueError(f"ANALYZE_MATRIX missing ml_id={ml_id}")
    return {f"analyze_{key}": bool(value) for key, value in matrix.items()}


def target_values_by_row(raw_row_json: str) -> dict[int, list[str]]:
    payload = parse_json_text(raw_row_json, {})
    by_row: dict[int, list[str]] = {54: [], 55: [], 56: [], 57: []}
    for column in payload.get("columns", []):
        for item in column.get("values", []):
            row_id = int(item.get("row_id"))
            if row_id not in by_row:
                continue
            text = clean_text(item.get("value"))
            if text is not None and text not in by_row[row_id]:
                by_row[row_id].append(text)
    return by_row


def join_unique(values: list[str]) -> str | None:
    if not values:
        return None
    return " / ".join(values)


def is_ubist_target_token(value: str | None) -> bool:
    return value is not None and UBIST_TARGET_PATTERN.search(value) is not None


def ubist_target_from_values(values: list[str]) -> str | None:
    return join_unique([value for value in values if is_ubist_target_token(value)])


def apply_target_policy(record: dict[str, Any], raw_targets: dict[int, list[str]]) -> None:
    """Apply Phase 14 Step 14-8 target policy in-place."""
    data_source = str(record["data_source"])

    if data_source in {"iqvia", "both"}:
        audit_codes: list[str | None] = list(AUDIT_CODES)
    else:
        audit_codes = [None, None, None]

    if data_source == "ubist":
        ubist_targets = [join_unique(raw_targets[row_id]) for row_id in (54, 55, 56, 57)]
    elif data_source == "both":
        ubist_targets = [
            ubist_target_from_values(raw_targets[row_id])
            for row_id in (54, 55, 56, 57)
        ]
    else:
        ubist_targets = [None, None, None, None]

    record.update(
        {
            "target_iqvia_1": audit_codes[0],
            "target_iqvia_2": audit_codes[1],
            "target_iqvia_3": audit_codes[2],
            "target_ubist_1": ubist_targets[0],
            "target_ubist_2": ubist_targets[1],
            "target_ubist_3": ubist_targets[2],
            "target_ubist_4": ubist_targets[3],
        }
    )


def make_record(
    ordinal: int,
    market_definition_row: dict[str, Any],
    master_drug_rows: list[dict[str, Any]],
    ingested_at: datetime,
) -> dict[str, Any]:
    strategic_market_id = str(market_definition_row["strategic_market_id"])
    ml_id = f"ml_{ordinal:03d}"
    data_source = normalize_data_source(
        market_definition_row.get("source_type"),
        strategic_market_id,
    )
    raw_targets = target_values_by_row(str(market_definition_row["raw_row_json"]))

    record: dict[str, Any] = {
        "ml_id": ml_id,
        "name": normalize_market_name(market_definition_row.get("market_name")),
        "data_source": data_source,
        "atc_codes_json": clean_text(market_definition_row.get("market_atc_codes_json")) or "[]",
        "source_file_version": clean_text(market_definition_row.get("source_file_version")),
        "ingested_at": ingested_at,
    }
    record.update(analyze_values_for_ml(ml_id))

    apply_target_policy(record, raw_targets)
    return {column: record.get(column) for column in ML_MARKET_COLUMNS}


def _source_file_version(rows: list[dict[str, Any]]) -> str:
    return source_file_version(rows, expected=EXPECTED_SOURCE_FILE_VERSION)


def load_existing_ml_market_records(
    existing_path: Path,
    ingested_at: datetime | None = None,
) -> list[dict[str, Any]]:
    rows = read_parquet_rows(existing_path)
    _source_file_version(rows)
    timestamp = ingested_at or utc_now_datetime()
    records: list[dict[str, Any]] = []
    for row in rows:
        record = {column: row.get(column) for column in ML_MARKET_COLUMNS}
        record["atc_codes_json"] = clean_text(record.get("atc_codes_json")) or "[]"
        record["ingested_at"] = timestamp
        raw_targets = {row_id: [] for row_id in (54, 55, 56, 57)}
        # Existing Phase 14 rows already contain normalized UBIST target slots.
        # D-45/Q-57 only need data_source-aware rewriting at this fallback stage.
        if str(record["data_source"]) == "ubist":
            raw_targets = {
                54: [clean_text(record.get("target_ubist_1"))] if clean_text(record.get("target_ubist_1")) else [],
                55: [clean_text(record.get("target_ubist_2"))] if clean_text(record.get("target_ubist_2")) else [],
                56: [clean_text(record.get("target_ubist_3"))] if clean_text(record.get("target_ubist_3")) else [],
                57: [clean_text(record.get("target_ubist_4"))] if clean_text(record.get("target_ubist_4")) else [],
            }
        elif str(record["data_source"]) == "both" and record["ml_id"] != "ml_015":
            raw_targets = {
                54: [clean_text(record.get("target_ubist_1"))] if clean_text(record.get("target_ubist_1")) else [],
                55: [clean_text(record.get("target_ubist_2"))] if clean_text(record.get("target_ubist_2")) else [],
                56: [clean_text(record.get("target_ubist_3"))] if clean_text(record.get("target_ubist_3")) else [],
                57: [clean_text(record.get("target_ubist_4"))] if clean_text(record.get("target_ubist_4")) else [],
            }
        record.update(analyze_values_for_ml(str(record["ml_id"])))
        apply_target_policy(record, raw_targets)
        records.append({column: record.get(column) for column in ML_MARKET_COLUMNS})
    validate_records(records)
    return records


def load_ml_market_records(
    market_definition_path: Path,
    master_drug_path: Path,
    existing_path: Path = DEFAULT_OUTPUT_FILE,
    ingested_at: datetime | None = None,
) -> list[dict[str, Any]]:
    if not market_definition_path.exists() or not master_drug_path.exists():
        if not existing_path.exists():
            raise FileNotFoundError(
                "Phase 14 source parquet files are missing and existing ml_market "
                f"fallback was not found: {existing_path}"
            )
        return load_existing_ml_market_records(existing_path, ingested_at)

    market_definition_rows = read_parquet_rows(market_definition_path)
    master_drug_rows = read_parquet_rows(master_drug_path)
    _source_file_version(market_definition_rows)
    _source_file_version(master_drug_rows)

    market_definition_by_id = {
        str(row.get("strategic_market_id")): row for row in market_definition_rows
    }
    actual_ids = set(market_definition_by_id)
    expected_ids = set(EXPECTED_MARKET_IDS)
    if actual_ids != expected_ids:
        raise ValueError(
            f"market_definition strategic_market_id mismatch: "
            f"missing={sorted(expected_ids - actual_ids)}, extra={sorted(actual_ids - expected_ids)}"
        )

    timestamp = ingested_at or utc_now_datetime()
    records = [
        make_record(
            ordinal=index,
            market_definition_row=market_definition_by_id[strategic_market_id],
            master_drug_rows=master_drug_rows,
            ingested_at=timestamp,
        )
        for index, strategic_market_id in enumerate(EXPECTED_MARKET_IDS, start=1)
    ]
    validate_records(records)
    return records
