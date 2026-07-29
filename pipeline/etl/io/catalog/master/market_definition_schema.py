from __future__ import annotations

from pathlib import Path

from pipeline.etl.lib.storage import get_mi_master_path
from pipeline.etl.mi_master_registry import (
    default_mi_master_registry,
)

DEFAULT_INPUT_FILE = get_mi_master_path()
DEFAULT_OUTPUT_FILE = Path("parquet/master_market_definition/master_market_definition.parquet")
SOURCE_SHEET = "시장정의 & Target"
_REGISTRY = default_mi_master_registry()
EXPECTED_ROW_COUNT = len(_REGISTRY.market_sheets)

MASTER_MARKET_DEFINITION_COLUMNS = (
    "strategic_market_id",
    "market_name",
    "source_type",
    "market_atc_codes_json",
    "full_market_atc4_codes_json",
    "direct_competition_brands_json",
    "description",
    "analysis_levels_json",
    "analysis_level_funnel",
    "analysis_level_etc",
    "target_customer_priority_json",
    "raw_row_json",
    "source_sheet",
    "source_file_version",
    "ingested_at",
)

MARKET_BY_ID = {
    market.strategic_market_id: {
        "sheet_name": market.sheet_name,
        "source_type": market.catalog_source_type,
    }
    for market in _REGISTRY.market_sheets
}

# 1-based column indexes in sheet "시장정의 & Target".
MARKET_DEFINITION_COLUMNS = _REGISTRY.market_definition_columns

MARKET_DESCRIPTIONS = {
    "strategy_015": "IQVIA 기준 하모닐란과 엔커버 2개의 PRODUCT NAME KOR 에 대해 PACK DESC 를 하위분류로 4가지로 분석",
}

ANALYSIS_LEVEL_ROWS = {
    14: "Class",
    15: "Molecule",
    16: "Brand",
    17: "Dosage Form",
    18: "Strength",
    19: "Etc",
}
FULL_MARKET_ROWS = range(22, 45)
DIRECT_COMPETITION_ROWS = range(48, 51)
TARGET_CUSTOMER_ROWS = range(54, 58)
METRIC_ROWS = range(61, 65)

EXPECTED_STRATEGIC_MARKET_IDS = tuple(MARKET_DEFINITION_COLUMNS.keys())
