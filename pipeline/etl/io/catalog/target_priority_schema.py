from __future__ import annotations

from pathlib import Path

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
EXPECTED_ROW_COUNT = 84
EXPECTED_SOURCE_VIEW_COUNTS = {"UBIST": 40, "IQVIA": 44}
EXPECTED_SOURCE_TYPE_COUNTS = {
    "raw_from_sheet": 49,
    "auto_fill_top_n_by_sales": 35,
}
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

