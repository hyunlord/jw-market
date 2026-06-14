from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline.etl.lib.storage import get_mi_master_path
from pipeline.etl.io.catalog._lib.expected_counts import expected_int, expected_mapping

DEFAULT_INPUT_FILE = get_mi_master_path()
MASTER_ROOT = DEFAULT_INPUT_FILE.parent
DEFAULT_CATALOG_PATH = Path("docs/reference/master_column_mapping_catalog.md")
DEFAULT_OUTPUT_FILE = Path("parquet/master_drug/master_drug.parquet")

EXPECTED_ROW_COUNT = expected_int("master_drug.row_count")
EXPECTED_EXCLUDED_ROWS = expected_int("master_drug.excluded_rows")
EXPECTED_SOURCE_TYPE_DISTRIBUTION = expected_mapping("master_drug.source_type_distribution")

MASTER_DRUG_COLUMNS = (
    "strategic_market_id",
    "market_name",
    "source_type",
    "drug_index",
    "atc4_code",
    "atc4_desc",
    "molecule",
    "product_name",
    "manufacturer",
    "seller",
    "pack_desc",
    "nhi_type",
    "class",
    "class_2",
    "dosage_form",
    "administration_route",
    "strength",
    "strength_raw",
    "strength_raw_2",
    "formulation",
    "funnel",
    "ox_gx",
    "molecule_disease_definition",
    "composition_type",
    "drug_extra_json",
    "raw_row_json",
    "column_metadata_json",
    "source_sheet",
    "source_file_version",
    "source_row_id",
    "ingested_at",
)

JSON_COLUMNS = ("drug_extra_json", "raw_row_json", "column_metadata_json")


@dataclass(frozen=True)
class MarketSheetConfig:
    strategic_market_id: str
    sheet_name: str
    header_row: int
    source_type: str


@dataclass
class MarketDrugStats:
    strategic_market_id: str
    sheet_name: str
    header_row: int
    max_row: int
    max_col: int
    raw_rows_scanned: int = 0
    empty_rows: int = 0
    excluded_rows: int = 0
    staging_rows: int = 0


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
EXPECTED_MARKET_STATS = expected_mapping("master_drug.market_stats")
