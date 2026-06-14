from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any

from pipeline.etl.io.catalog._common import count_by, read_parquet_rows, utc_now_text, write_records_parquet

DEFAULT_MARKET_DEFINITION_FILE = Path("parquet/master_market_definition/master_market_definition.parquet")
DEFAULT_QA_FILE = Path("parquet/master_qa/master_qa.parquet")
DEFAULT_OUTPUT_FILE = Path("parquet/dim_jw_products/dim_jw_products.parquet")

EXPECTED_ROW_COUNT = 25
EXPECTED_SOURCE_FILE_VERSION = "MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx"

DIM_JW_PRODUCTS_COLUMNS = (
    "jw_product_id",
    "strategic_market_id",
    "market_name",
    "jw_product_name",
    "source_note",
    "source_file_version",
    "ingested_at",
)

EXPECTED_FINAL_ROWS = (
    ("strategy_001", "라베칸 라베칸듀오", "라베칸", "sheet split"),
    ("strategy_001", "라베칸 라베칸듀오", "라베칸듀오", "sheet split"),
    ("strategy_002", "제이클", "제이클", "sheet split"),
    ("strategy_003", "가드렛 가드메트", "가드렛", "sheet split (molecule market)"),
    ("strategy_003", "가드렛 가드메트", "가드메트", "sheet split (molecule market)"),
    ("strategy_004", "타발리스", "타발리스", "sheet split"),
    ("strategy_005", "시그마트", "시그마트", "sheet split"),
    ("strategy_006", "리바로 리바로젯", "리바로", "sheet split"),
    ("strategy_006", "리바로 리바로젯", "리바로젯", "sheet split"),
    ("strategy_007", "리바로페노", "리바로페노", "sheet split"),
    ("strategy_008", "리바로하이 리바로브이", "리바로하이", "sheet split"),
    ("strategy_008", "리바로하이 리바로브이", "리바로브이", "sheet split"),
    ("strategy_009", "트루패스 피나스타 제이다트", "트루패스", "sheet split"),
    ("strategy_009", "트루패스 피나스타 제이다트", "피나스타", "sheet split"),
    ("strategy_009", "트루패스 피나스타 제이다트", "제이다트", "sheet split"),
    ("strategy_010", "뉴트로진 모빌리아", "뉴트로진", "sheet split"),
    ("strategy_010", "뉴트로진 모빌리아", "모빌리아", "sheet split"),
    ("strategy_011", "악템라", "악템라", "sheet split"),
    ("strategy_012", "페린젝트 베노훼럼", "페린젝트", "sheet split"),
    ("strategy_012", "페린젝트 베노훼럼", "베노훼럼", "sheet split"),
    ("strategy_013", "헴리브라", "헴리브라", "sheet split"),
    ("strategy_014", "위너프 위너프A+", "위너프", "sheet split"),
    (
        "strategy_014",
        "위너프 위너프A+",
        "위너프에이플러스",
        "sheet split (renamed from 위너프A+)",
    ),
    ("strategy_015", "엔커버", "엔커버", "sheet split"),
    ("strategy_016", "플라주오피", "플라주오피", "sheet split"),
)

EXPECTED_MARKET_DISTRIBUTION = {
    "strategy_001": 2,
    "strategy_002": 1,
    "strategy_003": 2,
    "strategy_004": 1,
    "strategy_005": 1,
    "strategy_006": 2,
    "strategy_007": 1,
    "strategy_008": 2,
    "strategy_009": 3,
    "strategy_010": 2,
    "strategy_011": 1,
    "strategy_012": 2,
    "strategy_013": 1,
    "strategy_014": 2,
    "strategy_015": 1,
    "strategy_016": 1,
}


def jw_product_id(strategic_market_id: str, jw_product_name: str) -> str:
    digest = hashlib.sha256(jw_product_name.encode("utf-8")).hexdigest()[:16]
    return f"{strategic_market_id}:{digest}"


def source_note_for(strategic_market_id: str, jw_product_name: str) -> str:
    if strategic_market_id == "strategy_003":
        return "sheet split (molecule market)"
    if strategic_market_id == "strategy_014" and jw_product_name == "위너프에이플러스":
        return "sheet split (renamed from 위너프A+)"
    return "sheet split"


def _source_file_version_from_market_definition(market_definition_records: list[dict[str, Any]]) -> str:
    versions = {
        str(record.get("source_file_version"))
        for record in market_definition_records
        if record.get("source_file_version") not in (None, "")
    }
    if versions != {EXPECTED_SOURCE_FILE_VERSION}:
        raise ValueError(
            f"market_definition source_file_version mismatch: "
            f"expected={EXPECTED_SOURCE_FILE_VERSION!r}, actual={sorted(versions)}"
        )
    return EXPECTED_SOURCE_FILE_VERSION


def blank_record() -> dict[str, str | None]:
    return {column: None for column in DIM_JW_PRODUCTS_COLUMNS}


def make_record(
    strategic_market_id: str,
    market_name: str,
    jw_product_name: str,
    source_note: str,
    source_file_version: str,
    ingested_at: str,
) -> dict[str, str | None]:
    record = blank_record()
    record.update(
        {
            "jw_product_id": jw_product_id(strategic_market_id, jw_product_name),
            "strategic_market_id": strategic_market_id,
            "market_name": market_name,
            "jw_product_name": jw_product_name,
            "source_note": source_note,
            "source_file_version": source_file_version,
            "ingested_at": ingested_at,
        }
    )
    return record


def load_dim_jw_product_records(
    market_definition_path: Path,
    qa_path: Path | None = None,
    ingested_at: str | None = None,
) -> list[dict[str, str | None]]:
    market_definition_records = read_parquet_rows(market_definition_path)
    source_file_version = _source_file_version_from_market_definition(market_definition_records)
    timestamp = ingested_at or utc_now_text()

    market_by_id = {
        str(record.get("strategic_market_id")): record for record in market_definition_records
    }
    records: list[dict[str, str | None]] = []

    for strategic_market_id, expected_market_name, _, _ in EXPECTED_FINAL_ROWS:
        market_record = market_by_id.get(strategic_market_id)
        if market_record is None:
            raise ValueError(f"missing market_definition row: {strategic_market_id}")
        actual_market_name = str(market_record.get("market_name") or "")
        if actual_market_name != expected_market_name:
            raise ValueError(
                f"{strategic_market_id} market_name mismatch: "
                f"expected={expected_market_name!r}, actual={actual_market_name!r}"
            )

    for strategic_market_id in sorted(market_by_id):
        market_name = str(market_by_id[strategic_market_id].get("market_name") or "")
        if not market_name:
            raise ValueError(f"empty market_name: {strategic_market_id}")
        for token in market_name.split():
            jw_product_name = (
                "위너프에이플러스"
                if strategic_market_id == "strategy_014" and token == "위너프A+"
                else token
            )
            records.append(
                make_record(
                    strategic_market_id,
                    market_name,
                    jw_product_name,
                    source_note_for(strategic_market_id, jw_product_name),
                    source_file_version,
                    timestamp,
                )
            )

    validate_records(records)
    return records


def validate_records(records: list[dict[str, Any]]) -> None:
    if len(records) != EXPECTED_ROW_COUNT:
        raise ValueError(f"row count must be {EXPECTED_ROW_COUNT}, found {len(records)}")

    for index, record in enumerate(records, start=1):
        columns = tuple(record.keys())
        if columns != DIM_JW_PRODUCTS_COLUMNS:
            raise ValueError(
                f"row {index} columns mismatch: expected={DIM_JW_PRODUCTS_COLUMNS}, actual={columns}"
            )
        for column, value in record.items():
            if value is not None and not isinstance(value, str):
                raise ValueError(f"row {index} column {column} must be string/None, got {type(value)}")
        if not record.get("source_note"):
            raise ValueError(f"row {index} source_note must be non-empty")

    ids = [record["jw_product_id"] for record in records]
    if len(set(ids)) != EXPECTED_ROW_COUNT:
        raise ValueError("jw_product_id must be unique")

    natural_keys = [
        (record["strategic_market_id"], record["jw_product_name"]) for record in records
    ]
    if len(set(natural_keys)) != EXPECTED_ROW_COUNT:
        raise ValueError("(strategic_market_id, jw_product_name) must be unique")

    expected_rows = [
        {
            "jw_product_id": jw_product_id(strategic_market_id, jw_product_name),
            "strategic_market_id": strategic_market_id,
            "market_name": market_name,
            "jw_product_name": jw_product_name,
            "source_note": source_note,
        }
        for strategic_market_id, market_name, jw_product_name, source_note in EXPECTED_FINAL_ROWS
    ]
    actual_without_run_fields = [
        {
            "jw_product_id": record["jw_product_id"],
            "strategic_market_id": record["strategic_market_id"],
            "market_name": record["market_name"],
            "jw_product_name": record["jw_product_name"],
            "source_note": record["source_note"],
        }
        for record in records
    ]
    if actual_without_run_fields != expected_rows:
        raise ValueError(
            f"final 25-row list mismatch: expected={expected_rows}, "
            f"actual={actual_without_run_fields}"
        )

    market_distribution = count_by(records, "strategic_market_id")
    if market_distribution != EXPECTED_MARKET_DISTRIBUTION:
        raise ValueError(
            f"market distribution mismatch: expected={EXPECTED_MARKET_DISTRIBUTION}, "
            f"actual={market_distribution}"
        )

    if any(record["jw_product_name"] == "하모닐란" for record in records):
        raise ValueError("하모닐란 row must not exist in Phase 12 dim_jw_products")
    if any(record["jw_product_name"] == "위너프A+" for record in records):
        raise ValueError("위너프A+ token must be renamed to 위너프에이플러스")

    winnerf_aplus = [
        record for record in records
        if record["strategic_market_id"] == "strategy_014"
        and record["jw_product_name"] == "위너프에이플러스"
    ]
    if len(winnerf_aplus) != 1:
        raise ValueError(f"위너프에이플러스 row must exist exactly once, found={len(winnerf_aplus)}")
    if "renamed from 위너프A+" not in str(winnerf_aplus[0].get("source_note")):
        raise ValueError("위너프에이플러스 source_note must include rename evidence")


def write_parquet(records: list[dict[str, Any]], output_file: Path) -> None:
    write_records_parquet(
        records,
        DIM_JW_PRODUCTS_COLUMNS,
        output_file,
        compression_level=3,
        stringify=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market-definition", type=Path, default=DEFAULT_MARKET_DEFINITION_FILE)
    parser.add_argument("--qa", type=Path, default=DEFAULT_QA_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--ingested-at", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    records = load_dim_jw_product_records(args.market_definition, args.qa, args.ingested_at)
    write_parquet(records, args.output)

    print("prototype Phase 12 Round 3 dim_jw_products patch -> Parquet")
    print(f"rows={len(records)}")
    print(f"columns={len(DIM_JW_PRODUCTS_COLUMNS)}")
    print(f"output={args.output}")
    print(f"source_file_version={records[0]['source_file_version']}")
    print(f"ingested_at={records[0]['ingested_at']}")
    print("market_distribution:")
    for market_id, count in sorted(count_by(records, "strategic_market_id").items()):
        print(f"  {market_id}: {count}")
    print("validate_records: PASS")


if __name__ == "__main__":
    main()
