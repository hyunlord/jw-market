from __future__ import annotations

import argparse
import json
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from pipeline.etl.io.catalog._common import (
    clean_text,
    count_by,
    dumps_compact_json,
    read_parquet_rows,
    utc_now_text,
    write_records_parquet,
)

DEFAULT_MARKET_DEFINITION_FILE = Path("parquet/master_market_definition/master_market_definition.parquet")
DEFAULT_MASTER_DRUG_FILE = Path("parquet/master_drug/master_drug.parquet")
DEFAULT_OUTPUT_FILE = Path("parquet/dim_market_landscape/dim_market_landscape.parquet")

EXPECTED_SOURCE_FILE_VERSION = "MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx"
EXPECTED_MARKET_IDS = tuple(f"strategy_{index:03d}" for index in range(1, 17))
EXPECTED_MARKET_COUNTS = {
    "strategy_001": 358,
    "strategy_002": 45,
    "strategy_003": 82,
    "strategy_004": 10,
    "strategy_005": 294,
    "strategy_006": 1047,
    "strategy_007": 117,
    "strategy_008": 1081,
    "strategy_009": 405,
    "strategy_010": 10,
    "strategy_011": 26,
    "strategy_012": 76,
    "strategy_013": 14,
    "strategy_014": 331,
    "strategy_015": 4,
    "strategy_016": 52,
}
DEFAULT_SHEET_ALL_MARKETS = {"strategy_005", "strategy_011"}
EXPECTED_TOTAL_MASTER_DRUG_ROWS = 3952

DIM_MARKET_LANDSCAPE_COLUMNS = (
    "market_landscape_id",
    "strategic_market_id",
    "sheet_name",
    "product_name_kor_in_sheet",
    "atc4_code",
    "atc4_desc",
    "nhi_type",
    "data_source_type",
    "analysis_value_raw",
    "mkt_team_jwp_mkt",
    "ml_definition_type",
    "ml_atc_codes_json",
    "ml_brand_count",
    "ml_brand_list_json",
    "analysis_metrics_json",
    "source_file_version",
    "ingested_at",
)


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


def validate_records(
    records: list[dict[str, Any]],
    market_definition_rows: list[dict[str, Any]],
    master_drug_rows: list[dict[str, Any]],
) -> None:
    if len(records) != 16:
        raise ValueError(f"dim_market_landscape row count must be 16, found={len(records)}")

    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != DIM_MARKET_LANDSCAPE_COLUMNS:
            raise ValueError(
                f"row {index} columns mismatch: "
                f"expected={DIM_MARKET_LANDSCAPE_COLUMNS}, actual={tuple(record.keys())}"
            )
        for column, value in record.items():
            if value is not None and not isinstance(value, str):
                raise ValueError(
                    f"row {index} column {column} must be string/None, got={type(value)}"
                )

    market_landscape_ids = [record["market_landscape_id"] for record in records]
    expected_ml_ids = [f"ml_{index:03d}" for index in range(1, 17)]
    if market_landscape_ids != expected_ml_ids:
        raise ValueError(
            f"market_landscape_id sequence mismatch: expected={expected_ml_ids}, "
            f"actual={market_landscape_ids}"
        )
    if len(set(market_landscape_ids)) != 16:
        raise ValueError("market_landscape_id must be unique")

    market_definition_ids = {str(row.get("strategic_market_id")) for row in market_definition_rows}
    record_ids = [str(record["strategic_market_id"]) for record in records]
    if set(record_ids) != market_definition_ids:
        raise ValueError("strategic_market_id FK to master_market_definition failed")

    master_counts = Counter(str(row.get("strategic_market_id")) for row in master_drug_rows)
    if dict(sorted(master_counts.items())) != EXPECTED_MARKET_COUNTS:
        raise ValueError(
            f"master_drug market count mismatch: "
            f"expected={EXPECTED_MARKET_COUNTS}, actual={dict(sorted(master_counts.items()))}"
        )
    if sum(master_counts.values()) != EXPECTED_TOTAL_MASTER_DRUG_ROWS:
        raise ValueError(f"master_drug total row count mismatch: {sum(master_counts.values())}")

    record_counts = {
        str(record["strategic_market_id"]): int(str(record["ml_brand_count"]))
        for record in records
    }
    if record_counts != EXPECTED_MARKET_COUNTS:
        raise ValueError(
            f"ml_brand_count mismatch: expected={EXPECTED_MARKET_COUNTS}, actual={record_counts}"
        )
    if sum(record_counts.values()) != EXPECTED_TOTAL_MASTER_DRUG_ROWS:
        raise ValueError(f"ml_brand_count total mismatch: {sum(record_counts.values())}")

    definition_type_counts = Counter(str(record["ml_definition_type"]) for record in records)
    expected_definition_type_counts = {"atc_codes_explicit": 14, "default_sheet_all": 2}
    if dict(definition_type_counts) != expected_definition_type_counts:
        raise ValueError(
            f"ml_definition_type distribution mismatch: "
            f"expected={expected_definition_type_counts}, actual={dict(definition_type_counts)}"
        )
    default_ids = {
        str(record["strategic_market_id"])
        for record in records
        if record["ml_definition_type"] == "default_sheet_all"
    }
    if default_ids != DEFAULT_SHEET_ALL_MARKETS:
        raise ValueError(
            f"default_sheet_all markets mismatch: expected={sorted(DEFAULT_SHEET_ALL_MARKETS)}, "
            f"actual={sorted(default_ids)}"
        )

    master_product_by_key = {
        (str(row.get("strategic_market_id")), int(str(row.get("drug_index")))): clean_text(
            row.get("product_name")
        )
        for row in master_drug_rows
    }
    for record in records:
        strategic_market_id = str(record["strategic_market_id"])
        ml_brand_count = int(str(record["ml_brand_count"]))
        for json_column in (
            "ml_atc_codes_json",
            "ml_brand_list_json",
            "analysis_metrics_json",
        ):
            try:
                parsed = json.loads(str(record[json_column]))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{strategic_market_id} {json_column} invalid JSON: {exc}") from exc
            if json_column in ("ml_atc_codes_json", "analysis_metrics_json") and not isinstance(parsed, list):
                raise ValueError(f"{strategic_market_id} {json_column} must be a JSON array")

        brand_payload = json.loads(str(record["ml_brand_list_json"]))
        if brand_payload.get("row_count") != ml_brand_count:
            raise ValueError(
                f"{strategic_market_id} ml_brand_list_json row_count mismatch: "
                f"{brand_payload.get('row_count')} != {ml_brand_count}"
            )
        brands = brand_payload.get("brands")
        if not isinstance(brands, list) or len(brands) != ml_brand_count:
            raise ValueError(
                f"{strategic_market_id} ml_brand_list_json brands length mismatch: "
                f"{len(brands) if isinstance(brands, list) else 'not-list'} != {ml_brand_count}"
            )
        expected_indexes = list(range(1, ml_brand_count + 1))
        actual_indexes = [int(brand["drug_index"]) for brand in brands]
        if actual_indexes != expected_indexes:
            raise ValueError(
                f"{strategic_market_id} drug_index sequence mismatch in ml_brand_list_json"
            )
        for brand in brands:
            key = (strategic_market_id, int(brand["drug_index"]))
            if master_product_by_key.get(key) != brand.get("product_name"):
                raise ValueError(
                    f"{strategic_market_id} drug_index={brand['drug_index']} product_name mismatch: "
                    f"json={brand.get('product_name')!r}, master={master_product_by_key.get(key)!r}"
                )


def write_parquet(records: list[dict[str, Any]], output_file: Path) -> None:
    write_records_parquet(
        records,
        DIM_MARKET_LANDSCAPE_COLUMNS,
        output_file,
        compression_level=3,
        stringify=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Phase 12 dim_market_landscape parquet.")
    parser.add_argument("--market-definition", type=Path, default=DEFAULT_MARKET_DEFINITION_FILE)
    parser.add_argument("--master-drug", type=Path, default=DEFAULT_MASTER_DRUG_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--ingested-at", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    records = load_dim_market_landscape_records(args.market_definition, args.master_drug, args.ingested_at)
    write_parquet(records, args.output)

    print("prototype Phase 12 Round 4 dim_market_landscape -> Parquet")
    print(f"rows={len(records)}")
    print(f"columns={len(DIM_MARKET_LANDSCAPE_COLUMNS)}")
    print(f"output={args.output}")
    print(f"source_file_version={records[0]['source_file_version']}")
    print(f"ingested_at={records[0]['ingested_at']}")
    print("ml_definition_type_distribution:")
    for definition_type, count in sorted(count_by(records, "ml_definition_type").items()):
        print(f"  {definition_type}: {count}")
    print("ml_brand_count_by_market:")
    for record in records:
        print(f"  {record['strategic_market_id']}: {record['ml_brand_count']}")
    print("validate_records: PASS")


if __name__ == "__main__":
    main()
