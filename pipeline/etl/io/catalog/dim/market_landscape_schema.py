from __future__ import annotations

from pathlib import Path

from pipeline.etl.io.catalog._lib.expected_counts import expected_int, expected_mapping

DEFAULT_MARKET_DEFINITION_FILE = Path("parquet/master_market_definition/master_market_definition.parquet")
DEFAULT_MASTER_DRUG_FILE = Path("parquet/master_drug/master_drug.parquet")
DEFAULT_OUTPUT_FILE = Path("parquet/dim_market_landscape/dim_market_landscape.parquet")

EXPECTED_SOURCE_FILE_VERSION = "MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx"
EXPECTED_ROW_COUNT = expected_int("dim_market_landscape.row_count")
EXPECTED_MARKET_COUNTS = expected_mapping("dim_market_landscape.market_counts")
EXPECTED_MARKET_IDS = tuple(EXPECTED_MARKET_COUNTS)
DEFAULT_SHEET_ALL_MARKETS = {"strategy_005", "strategy_011"}
EXPECTED_TOTAL_MASTER_DRUG_ROWS = expected_int("dim_market_landscape.total_master_drug_rows")

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

EXPECTED_DEFINITION_TYPE_COUNTS = expected_mapping("dim_market_landscape.definition_type_counts")
