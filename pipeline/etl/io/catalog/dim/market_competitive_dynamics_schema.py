from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.etl.io.catalog._lib.expected_counts import expected_int, expected_mapping
from pipeline.etl.mi_master_registry import (
    MiMasterRegistry,
    default_mi_master_registry,
)

DEFAULT_DIM_MARKET_LANDSCAPE_FILE = Path("parquet/dim_market_landscape/dim_market_landscape.parquet")
DEFAULT_MARKET_DEFINITION_FILE = Path("parquet/master_market_definition/master_market_definition.parquet")
DEFAULT_MASTER_DRUG_FILE = Path("parquet/master_drug/master_drug.parquet")
DEFAULT_OUTPUT_FILE = Path("parquet/dim_market_competitive_dynamics/dim_market_competitive_dynamics.parquet")

EXPECTED_SOURCE_FILE_VERSION = "MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx"

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


def competitive_dynamics_contract(
    registry: MiMasterRegistry | None = None,
) -> tuple[tuple[str, ...], int]:
    active_registry = registry or default_mi_master_registry()
    cd_ids = tuple(str(spec["cd_id"]) for spec in active_registry.cd_specs)
    return cd_ids, len(cd_ids)


EXPECTED_CD_IDS, EXPECTED_ROW_COUNT = competitive_dynamics_contract()
EXPECTED_CD_COUNTS = expected_mapping("dim_market_competitive_dynamics.cd_counts")
EXPECTED_TOTAL_CD_BRAND_COUNT = expected_int("dim_market_competitive_dynamics.total_cd_brand_count")
EXPECTED_STRATEGY_008_CLASS2_NON_NULL_COUNT = expected_int(
    "dim_market_competitive_dynamics.strategy_008_class2_non_null_count"
)
EXPECTED_STRATEGY_008_NON_CD_CLASS2_COUNT = expected_int(
    "dim_market_competitive_dynamics.strategy_008_non_cd_class2_count"
)
EXPECTED_DEFINITION_TYPE_COUNTS = expected_mapping("dim_market_competitive_dynamics.definition_type_counts")
