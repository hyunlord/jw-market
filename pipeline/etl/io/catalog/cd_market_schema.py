from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa

DEFAULT_ML_MARKET_FILE = Path("parquet/ml_market/ml_market.parquet")
DEFAULT_CD_FILTER_FILE = Path("parquet/cd_filter/cd_filter.parquet")
DEFAULT_MARKET_DEFINITION_FILE = Path(
    "parquet/master_market_definition/master_market_definition.parquet"
)
DEFAULT_OUTPUT_FILE = Path("parquet/cd_market/cd_market.parquet")

# cd_market 19개 정의도 260518 MI Master가 기준이다.
# 이 상수는 CD 시장 수, target priority, view_source_id 추적을 같은 원본으로
# 묶기 위한 checkpoint다. 파일명 mismatch를 조용히 통과시키는 대안은 운영
# smoke에서 CD tooltip/시장정의 원인을 역추적하기 어렵게 하므로 기각했다.
EXPECTED_SOURCE_FILE_VERSION = "MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx"
EXPECTED_CD_IDS = tuple(f"cd_{index:03d}" for index in range(1, 20))
EXPECTED_DATA_SOURCE_COUNTS = {"both": 2, "iqvia": 9, "ubist": 8}
COLLAPSE_PAIR_CD_ID = "cd_015"
CD_SPECIFIC_ROWS_TO_VALIDATE = (
    14,
    15,
    17,
    18,
    19,
    54,
    55,
    56,
    57,
)

CD_MARKET_COLUMNS = (
    "cd_id",
    "name",
    "ml_id",
    "cd_filter_id",
    "data_source",
    "analyze_class",
    "analyze_molecule",
    "analyze_dosage_form",
    "analyze_strength_pack",
    "analyze_nhi_type",
    "analyze_ox_gx",
    "analyze_fish_oil",
    "target_iqvia_1",
    "target_iqvia_2",
    "target_iqvia_3",
    "target_ubist_1",
    "target_ubist_2",
    "target_ubist_3",
    "target_ubist_4",
    "source_file_version",
    "ingested_at",
)

CD_MARKET_SCHEMA = pa.schema(
    [
        pa.field("cd_id", pa.string(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        pa.field("ml_id", pa.string(), nullable=False),
        pa.field("cd_filter_id", pa.string(), nullable=False),
        pa.field("data_source", pa.string(), nullable=False),
        pa.field("analyze_class", pa.bool_(), nullable=False),
        pa.field("analyze_molecule", pa.bool_(), nullable=False),
        pa.field("analyze_dosage_form", pa.bool_(), nullable=False),
        pa.field("analyze_strength_pack", pa.bool_(), nullable=False),
        pa.field("analyze_nhi_type", pa.bool_(), nullable=False),
        pa.field("analyze_ox_gx", pa.bool_(), nullable=False),
        pa.field("analyze_fish_oil", pa.bool_(), nullable=False),
        pa.field("target_iqvia_1", pa.string(), nullable=True),
        pa.field("target_iqvia_2", pa.string(), nullable=True),
        pa.field("target_iqvia_3", pa.string(), nullable=True),
        pa.field("target_ubist_1", pa.string(), nullable=True),
        pa.field("target_ubist_2", pa.string(), nullable=True),
        pa.field("target_ubist_3", pa.string(), nullable=True),
        pa.field("target_ubist_4", pa.string(), nullable=True),
        pa.field("source_file_version", pa.string(), nullable=False),
        pa.field("ingested_at", pa.timestamp("us"), nullable=False),
    ]
)

CD_SPECS: tuple[dict[str, Any], ...] = (
    {"cd_id": "cd_001", "name": "라베칸 라베칸듀오", "ml_id": "ml_001", "cd_filter_id": "cdf_001", "strategic_market_id": "strategy_001", "column_ids": (3,)},
    {"cd_id": "cd_002", "name": "제이클", "ml_id": "ml_002", "cd_filter_id": "cdf_002", "strategic_market_id": "strategy_002", "column_ids": (4,)},
    {"cd_id": "cd_003", "name": "가드렛 가드메트", "ml_id": "ml_003", "cd_filter_id": "cdf_003", "strategic_market_id": "strategy_003", "column_ids": (5,)},
    {"cd_id": "cd_004", "name": "타발리스", "ml_id": "ml_004", "cd_filter_id": "cdf_004", "strategic_market_id": "strategy_004", "column_ids": (6,)},
    {"cd_id": "cd_005", "name": "시그마트", "ml_id": "ml_005", "cd_filter_id": "cdf_005", "strategic_market_id": "strategy_005", "column_ids": (7,)},
    {"cd_id": "cd_006", "name": "리바로 리바로젯", "ml_id": "ml_006", "cd_filter_id": "cdf_006", "strategic_market_id": "strategy_006", "column_ids": (8,)},
    {"cd_id": "cd_007", "name": "리바로페노", "ml_id": "ml_007", "cd_filter_id": "cdf_007", "strategic_market_id": "strategy_007", "column_ids": (9,)},
    {"cd_id": "cd_008", "name": "리바로하이", "ml_id": "ml_008", "cd_filter_id": "cdf_008", "strategic_market_id": "strategy_008", "column_ids": (10,)},
    {"cd_id": "cd_009", "name": "리바로브이", "ml_id": "ml_008", "cd_filter_id": "cdf_009", "strategic_market_id": "strategy_008", "column_ids": (11,)},
    {"cd_id": "cd_010", "name": "트루패스", "ml_id": "ml_009", "cd_filter_id": "cdf_010", "strategic_market_id": "strategy_009", "column_ids": (12,)},
    {"cd_id": "cd_011", "name": "피나스타 제이다트", "ml_id": "ml_009", "cd_filter_id": "cdf_011", "strategic_market_id": "strategy_009", "column_ids": (13,)},
    {"cd_id": "cd_012", "name": "뉴트로진", "ml_id": "ml_010", "cd_filter_id": "cdf_012", "strategic_market_id": "strategy_010", "column_ids": (14,)},
    {"cd_id": "cd_013", "name": "모빌리아", "ml_id": "ml_010", "cd_filter_id": "cdf_013", "strategic_market_id": "strategy_010", "column_ids": (15,)},
    {"cd_id": "cd_014", "name": "악템라", "ml_id": "ml_011", "cd_filter_id": "cdf_014", "strategic_market_id": "strategy_011", "column_ids": (16,)},
    {"cd_id": "cd_015", "name": "페린젝트 베노훼럼", "ml_id": "ml_012", "cd_filter_id": "cdf_015", "strategic_market_id": "strategy_012", "column_ids": (17, 18)},
    {"cd_id": "cd_016", "name": "헴리브라", "ml_id": "ml_013", "cd_filter_id": "cdf_016", "strategic_market_id": "strategy_013", "column_ids": (19,)},
    {"cd_id": "cd_017", "name": "엔커버", "ml_id": "ml_015", "cd_filter_id": "cdf_017", "strategic_market_id": "strategy_015", "column_ids": (20,)},
    {"cd_id": "cd_018", "name": "위너프 위너프에이플러스", "ml_id": "ml_014", "cd_filter_id": "cdf_018", "strategic_market_id": "strategy_014", "column_ids": (21,)},
    {"cd_id": "cd_019", "name": "플라주오피", "ml_id": "ml_016", "cd_filter_id": "cdf_019", "strategic_market_id": "strategy_016", "column_ids": (22,)},
)
