from __future__ import annotations

from pathlib import Path

from pipeline.etl.io.catalog._lib.expected_counts import expected_int, expected_mapping

DEFAULT_SKELETON_FILE = Path(
    "data/cache/prototype_11_step_c4_target_priority_precompute_sample.csv"
)
DEFAULT_DIM_COMPETITIVE_FILE = Path(
    "parquet/dim_market_competitive_dynamics/dim_market_competitive_dynamics.parquet"
)
DEFAULT_MASTER_DRUG_FILE = Path("parquet/master_drug/master_drug.parquet")
DEFAULT_UBIST_BASE_DIR = Path("output/ubist")
DEFAULT_IQVIA_DIR = Path("output/iqvia_nsa")
DEFAULT_OUTPUT_FILE = Path(
    "parquet/dim_market_target_priority/dim_market_target_priority.parquet"
)
DEFAULT_CACHE_FILE = Path(
    "data/cache/prototype_12_round6_auto_fill_customer_dictionary_estimate.csv"
)

# target priority와 시장정의 & Target 항목도 260518 파일 기준으로 검증한다.
# A10N1/A10P1처럼 target ATC 설명이 바뀐 행이 있어, source version을 남겨야
# downstream smoke에서 어떤 정의로 만든 payload인지 역추적할 수 있다.
# 설명만 최신으로 바꾸고 산출은 구버전으로 두는 대안은 감사 불가능하므로 기각했다.
EXPECTED_SOURCE_FILE_VERSION = "MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx"
LEGACY_SKELETON_SOURCE_FILE_VERSION = "MI팀_시장분석 AI_시장 분석 Master Version (260422).xlsx"
EXPECTED_ROW_COUNT = expected_int("dim_market_target_priority.row_count")
EXPECTED_SOURCE_VIEW_COUNTS = expected_mapping("dim_market_target_priority.source_view_counts")
EXPECTED_SOURCE_TYPE_COUNTS = expected_mapping("dim_market_target_priority.source_type_counts")
EXPECTED_BOTH_SOURCE_VIEW_CDS = {"cd_003", "cd_017"}

DIM_MARKET_TARGET_PRIORITY_COLUMNS = (
    "target_priority_id",
    "competitive_dynamics_id",
    "source_view",
    "priority_rank",
    "target_customer",
    "source_type",
    "source_evidence",
    "source_file_version",
    "ingested_at",
)

AUTO_FILL_CACHE_COLUMNS = (
    "target_priority_id",
    "competitive_dynamics_id",
    "source_view",
    "priority_rank",
    "sales_rank",
    "target_customer",
    "rank_available",
    "source_partition",
    "ranking_basis",
    "join_key",
    "sales_amount",
    "sales_rows",
    "matched_sales_rows",
    "available_customer_groups",
    "estimate_status",
    "source_evidence",
)

UBIST_LATEST_PARTITION = "latest"
IQVIA_LATEST_PARTITION = "latest"

# Q-34 / C-4b source-specific dictionary.
UBIST_JOIN_VALUE_COLUMN_BY_SMID = {
    "strategy_001": "brand",
    "strategy_003": "brand",
    "strategy_005": "brand",
    "strategy_006": "product_name",
    "strategy_007": "product_name",
    "strategy_008": "brand",
    "strategy_009": "brand",
    "strategy_015": "brand",
}

from collections import Counter, defaultdict
from typing import Any


def validate_records(
    records: list[dict[str, Any]],
    dim_competitive_rows: list[dict[str, Any]],
    cache_rows: list[dict[str, Any]],
) -> None:
    if len(records) != EXPECTED_ROW_COUNT:
        raise ValueError(f"row count must be {EXPECTED_ROW_COUNT}, found={len(records)}")
    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != DIM_MARKET_TARGET_PRIORITY_COLUMNS:
            raise ValueError(
                f"row {index} columns mismatch: expected={DIM_MARKET_TARGET_PRIORITY_COLUMNS}, "
                f"actual={tuple(record.keys())}"
            )
        for column, value in record.items():
            if value is not None and not isinstance(value, str):
                raise ValueError(f"row {index} column {column} must be string/None, got={type(value)}")

    target_ids = [record["target_priority_id"] for record in records]
    expected_ids = [f"tp_{index:03d}" for index in range(1, EXPECTED_ROW_COUNT + 1)]
    if target_ids != expected_ids:
        raise ValueError(f"target_priority_id sequence mismatch: {target_ids}")
    if len(set(target_ids)) != EXPECTED_ROW_COUNT:
        raise ValueError("target_priority_id must be unique")

    unique_keys = [
        (
            record["competitive_dynamics_id"],
            record["source_view"],
            record["priority_rank"],
        )
        for record in records
    ]
    if len(set(unique_keys)) != EXPECTED_ROW_COUNT:
        duplicates = [key for key, count in Counter(unique_keys).items() if count > 1]
        raise ValueError(f"(cd_id, source_view, priority_rank) duplicates: {duplicates}")

    cd_ids = {str(row["competitive_dynamics_id"]) for row in dim_competitive_rows}
    for record in records:
        if record["competitive_dynamics_id"] not in cd_ids:
            raise ValueError(f"missing competitive_dynamics FK: {record['competitive_dynamics_id']}")

    source_view_counts = dict(Counter(record["source_view"] for record in records))
    if source_view_counts != EXPECTED_SOURCE_VIEW_COUNTS:
        raise ValueError(
            f"source_view distribution mismatch: expected={EXPECTED_SOURCE_VIEW_COUNTS}, "
            f"actual={source_view_counts}"
        )
    source_type_counts = dict(Counter(record["source_type"] for record in records))
    if source_type_counts != EXPECTED_SOURCE_TYPE_COUNTS:
        raise ValueError(
            f"source_type distribution mismatch: expected={EXPECTED_SOURCE_TYPE_COUNTS}, "
            f"actual={source_type_counts}"
        )

    ranks_by_cd_source: dict[tuple[str, str], list[str]] = defaultdict(list)
    source_views_by_cd: dict[str, set[str]] = defaultdict(set)
    for record in records:
        cd_id = str(record["competitive_dynamics_id"])
        source_view = str(record["source_view"])
        ranks_by_cd_source[(cd_id, source_view)].append(str(record["priority_rank"]))
        source_views_by_cd[cd_id].add(source_view)
    for key, ranks in ranks_by_cd_source.items():
        if sorted(ranks, key=int) != ["1", "2", "3", "4"]:
            raise ValueError(f"priority_rank must be 1-4 for {key}: {ranks}")
    both_source_view_cds = {
        cd_id for cd_id, source_views in source_views_by_cd.items() if len(source_views) == 2
    }
    if both_source_view_cds != EXPECTED_BOTH_SOURCE_VIEW_CDS:
        raise ValueError(
            f"BOTH source_view CD mismatch: expected={EXPECTED_BOTH_SOURCE_VIEW_CDS}, "
            f"actual={both_source_view_cds}"
        )

    if len(cache_rows) != EXPECTED_SOURCE_TYPE_COUNTS["auto_fill_top_n_by_sales"]:
        raise ValueError(f"auto-fill cache row count mismatch: {len(cache_rows)}")
    for cache_row in cache_rows:
        if set(cache_row.keys()) != set(AUTO_FILL_CACHE_COLUMNS):
            raise ValueError(f"auto-fill cache shape mismatch: {cache_row.keys()}")
