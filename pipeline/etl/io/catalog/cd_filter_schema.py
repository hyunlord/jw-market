from __future__ import annotations

from pathlib import Path

import pyarrow as pa

DEFAULT_MARKET_DEFINITION_FILE = Path(
    "parquet/master_market_definition/master_market_definition.parquet"
)
DEFAULT_OUTPUT_FILE = Path("parquet/cd_filter/cd_filter.parquet")

# cd_filter는 CD narrower universe를 결정하는 핵심 입력이다.
# 260518 migration 뒤에는 ML과 CD가 같은 원본 버전에서 갈라져야 하므로,
# source_file_version을 여기서도 강제한다. CD filter만 과거 파일을 허용하는
# 방식은 rank/market size drift를 만든 경험이 있어 기각했다.
EXPECTED_SOURCE_FILE_VERSION = "MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx"
EXPECTED_CD_FILTER_IDS = tuple(f"cdf_{index:03d}" for index in range(1, 20))
ML_EQUALS_CD_FILTER_IDS = {"cdf_004", "cdf_006", "cdf_007", "cdf_014", "cdf_016", "cdf_017"}
JSON_ARRAY_COLUMNS = ("atc3", "atc4", "molecule", "class")
FILTER_COLUMNS = ("atc3", "atc4", "molecule", "class", "nhi", "dosage_form")

CD_FILTER_COLUMNS = (
    "cd_filter_id",
    "name",
    "atc3",
    "atc4",
    "molecule",
    "class",
    "nhi",
    "dosage_form",
    "source_file_version",
    "ingested_at",
)

CD_FILTER_SCHEMA = pa.schema(
    [
        pa.field("cd_filter_id", pa.string(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        pa.field("atc3", pa.string(), nullable=True),
        pa.field("atc4", pa.string(), nullable=True),
        pa.field("molecule", pa.string(), nullable=True),
        pa.field("class", pa.string(), nullable=True),
        pa.field("nhi", pa.string(), nullable=True),
        pa.field("dosage_form", pa.string(), nullable=True),
        pa.field("source_file_version", pa.string(), nullable=False),
        pa.field("ingested_at", pa.timestamp("us"), nullable=False),
    ]
)
