from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline.etl.lib.storage import get_mi_master_path
from pipeline.etl.io.catalog._lib.expected_counts import expected_int, expected_mapping

DEFAULT_INPUT_FILE = get_mi_master_path()
MASTER_ROOT = DEFAULT_INPUT_FILE.parent
DEFAULT_CATALOG_PATH = Path("docs/reference/master_column_mapping_catalog.md")
DEFAULT_OUTPUT_FILE = Path("parquet/master_mapping_table/master_mapping_table.parquet")

# 4/22 기준 5932행에서 260518 기준 5956행으로 24행이 늘었다.
# diff 확인 결과 시장정의/Target 계열의 정상 추가분이라, 검증을 완화하지 않고
# 새 원본 버전에 맞춘 strict count로 고정한다. 행수 검사를 제거하는 대안은
# mapping 누락을 조기에 잡지 못하므로 기각했다.
EXPECTED_ROW_COUNT = expected_int("master_mapping_table.row_count")
ZERO_MAPPING_MARKETS = {"strategy_006", "strategy_007", "strategy_009"}

MASTER_MAPPING_TABLE_COLUMNS = (
    "mapping_id",
    "strategic_market_id",
    "source_value",
    "target_column",
    "target_value",
    "mapping_type",
    "source_sheet",
    "source_file_version",
    "ingested_at",
)


@dataclass(frozen=True)
class MarketSheetConfig:
    strategic_market_id: str
    sheet_name: str
    header_row: int
    source_type: str


MARKET_SHEETS: tuple[MarketSheetConfig, ...] = (
    MarketSheetConfig("strategy_001", "라베칸 라베칸듀오", 5, "UBIST"),
    MarketSheetConfig("strategy_002", "제이클", 5, "IQVIA"),
    MarketSheetConfig("strategy_003", "가드렛 가드메트", 5, "IQVIA"),
    MarketSheetConfig("strategy_004", "타발리스", 5, "IQVIA"),
    MarketSheetConfig("strategy_005", "시그마트", 5, "UBIST"),
    MarketSheetConfig("strategy_006", "리바로 리바로젯", 4, "UBIST"),
    MarketSheetConfig("strategy_007", "리바로페노", 4, "UBIST"),
    MarketSheetConfig("strategy_008", "리바로하이 리바로브이", 5, "UBIST"),
    MarketSheetConfig("strategy_009", "트루패스 피나스타 제이다트", 5, "UBIST"),
    MarketSheetConfig("strategy_010", "뉴트로진 모빌리아", 5, "IQVIA"),
    MarketSheetConfig("strategy_011", "악템라", 5, "IQVIA"),
    MarketSheetConfig("strategy_012", "페린젝트 베노훼럼", 5, "IQVIA"),
    MarketSheetConfig("strategy_013", "헴리브라", 5, "IQVIA"),
    MarketSheetConfig("strategy_014", "위너프 위너프A+", 5, "IQVIA"),
    MarketSheetConfig("strategy_015", "엔커버", 7, "IQVIA"),
    MarketSheetConfig("strategy_016", "플라주오피", 5, "IQVIA"),
)

MARKET_SHEET_BY_ID = {config.strategic_market_id: config for config in MARKET_SHEETS}
EXPECTED_MARKET_STATS = expected_mapping("master_mapping_table.market_stats")
EXPECTED_MARKET_DISTRIBUTION = expected_mapping("master_mapping_table.market_distribution")
EXPECTED_MAPPING_TYPE_DISTRIBUTION = expected_mapping("master_mapping_table.mapping_type_distribution")


@dataclass
class MarketMappingStats:
    strategic_market_id: str
    sheet_name: str
    header_row: int
    max_row: int
    max_col: int
    raw_rows_scanned: int = 0
    empty_rows: int = 0
    excluded_rows: int = 0
    staging_rows: int = 0
    manual_specs: int = 0
    mapping_rows: int = 0
