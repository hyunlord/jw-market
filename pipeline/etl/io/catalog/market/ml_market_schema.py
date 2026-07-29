from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pyarrow as pa

from pipeline.etl.mi_master_registry import (
    default_mi_master_registry,
)

DEFAULT_MARKET_DEFINITION_FILE = Path(
    "parquet/master_market_definition/master_market_definition.parquet"
)
DEFAULT_MASTER_DRUG_FILE = Path("parquet/master_drug/master_drug.parquet")
DEFAULT_OUTPUT_FILE = Path("parquet/ml_market/ml_market.parquet")

# ml_market는 전략뷰 Market Landscape의 envelope 정의다.
# 이번 rebuild의 기준은 260518 MI Master이므로, 여기서도 같은 파일명을
# source_file_version으로 요구한다. market metadata만 최신으로 바꾸고
# ml_market parquet가 4/22이면 원인분석/시장현황이 다른 시장정의를 보게 되어
# 기각한다.
EXPECTED_SOURCE_FILE_VERSION = "MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx"
EXPECTED_STRATEGY_005_SOURCE = "ubist"

ANALYZE_COLUMNS = (
    "analyze_class",
    "analyze_molecule",
    "analyze_dosage_form",
    "analyze_strength_pack",
    "analyze_nhi_type",
    "analyze_ox_gx",
    "analyze_fish_oil",
)

# Analysis axes are derived from MI Master markers and detail-sheet headers.
# Non-inferable business decisions live in mi_master_rules.yaml.
ANALYZE_MATRIX: dict[str, dict[str, bool]] = (
    default_mi_master_registry().analyze_matrix
)
EXPECTED_ML_IDS = tuple(ANALYZE_MATRIX)
EXPECTED_DATA_SOURCE_COUNTS = dict(
    Counter(
        sheet.source_type.lower()
        for sheet in default_mi_master_registry().market_sheets
    )
)
EXPECTED_MARKET_IDS = tuple(
    f"strategy_{ml_id.removeprefix('ml_')}" for ml_id in EXPECTED_ML_IDS
)
ML_MARKET_COLUMNS = (
    "ml_id",
    "name",
    "data_source",
    "atc_codes_json",
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

ML_MARKET_SCHEMA = pa.schema(
    [
        pa.field("ml_id", pa.string(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        pa.field("data_source", pa.string(), nullable=False),
        pa.field("atc_codes_json", pa.string(), nullable=False),
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

AUDIT_CODES = ("KHPA", "KCPA", "KPA")
UBIST_TARGET_PATTERN = re.compile(r"^(GH|CL)\s+\S+", re.IGNORECASE)
