from __future__ import annotations

from pathlib import Path

from pipeline.etl.lib.storage import get_mi_master_path
from pipeline.etl.io.catalog._lib.expected_counts import expected_int

DEFAULT_INPUT_FILE = get_mi_master_path()
DEFAULT_OUTPUT_FILE = Path("parquet/master_market_definition/master_market_definition.parquet")
SOURCE_SHEET = "시장정의 & Target"
EXPECTED_ROW_COUNT = expected_int("master_market_definition.row_count")

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
    "strategy_001": {"sheet_name": "라베칸 라베칸듀오", "source_type": "UBIST"},
    "strategy_002": {"sheet_name": "제이클", "source_type": "IQVIA"},
    "strategy_003": {"sheet_name": "가드렛 가드메트", "source_type": "IQVIA"},
    "strategy_004": {"sheet_name": "타발리스", "source_type": "IQVIA"},
    "strategy_005": {"sheet_name": "시그마트", "source_type": "IQVIA"},
    "strategy_006": {"sheet_name": "리바로 리바로젯", "source_type": "UBIST"},
    "strategy_007": {"sheet_name": "리바로페노", "source_type": "UBIST"},
    "strategy_008": {"sheet_name": "리바로하이 리바로브이", "source_type": "UBIST"},
    "strategy_009": {"sheet_name": "트루패스 피나스타 제이다트", "source_type": "UBIST"},
    "strategy_010": {"sheet_name": "뉴트로진 모빌리아", "source_type": "IQVIA"},
    "strategy_011": {"sheet_name": "악템라", "source_type": "IQVIA"},
    "strategy_012": {"sheet_name": "페린젝트 베노훼럼", "source_type": "IQVIA"},
    "strategy_013": {"sheet_name": "헴리브라", "source_type": "IQVIA"},
    "strategy_014": {"sheet_name": "위너프 위너프A+", "source_type": "IQVIA"},
    "strategy_015": {"sheet_name": "엔커버", "source_type": "IQVIA"},
    "strategy_016": {"sheet_name": "플라주오피", "source_type": "IQVIA"},
}

# 1-based column indexes in sheet "시장정의 & Target".
MARKET_DEFINITION_COLUMNS: dict[str, tuple[int, ...]] = {
    "strategy_001": (3,),
    "strategy_002": (4,),
    "strategy_003": (5,),
    "strategy_004": (6,),
    "strategy_005": (7,),
    "strategy_006": (8,),
    "strategy_007": (9,),
    "strategy_008": (10, 11),
    "strategy_009": (12, 13),
    "strategy_010": (14, 15),
    "strategy_011": (16,),
    "strategy_012": (17, 18),
    "strategy_013": (19,),
    "strategy_015": (20,),
    "strategy_014": (21,),
    "strategy_016": (22,),
}

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
