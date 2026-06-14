from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline.etl.lib.storage import get_mi_master_path
from pipeline.etl.io.catalog._lib.expected_counts import expected_int, expected_mapping

DEFAULT_INPUT_FILE = get_mi_master_path()
DEFAULT_OUTPUT_FILE = Path("parquet/master_brand_consolidation/master_brand_consolidation.parquet")

STRATEGIC_MARKET_ID = "strategy_011"
SOURCE_SHEET = "악템라"
HEADER_ROW = 5
PRODUCT_NAME_SOURCE_COLUMN = "PRODUCT NAME KOR"
# 260518 악템라 시트는 Excel formatting tail이 길게 남아 raw scan 기준으로는
# 995행까지 보이지만 실제 staging 대상은 26개 약품 행이다. 따라서 raw-scanned
# exact count가 아니라 staging drug row, consolidation 6행, member index
# uniqueness를 불변량으로 둔다. 빈 tail을 행으로 취급하는 대안은 무의미한
# 공백 데이터를 catalog에 끌어들이므로 기각했다.
EXPECTED_DRUG_ROWS = expected_int("master_brand_consolidation.staging_drug_rows")
EXPECTED_ROW_COUNT = expected_int("master_brand_consolidation.row_count")
EXPECTED_MEMBER_DRUG_INDEXES = {5, 6, 18, 19, 22, 23}
SOURCE_REMARK = "Master Remark indicates one-brand consolidation"

MASTER_BRAND_CONSOLIDATION_COLUMNS = (
    "strategic_market_id",
    "brand_group",
    "member_drug_index",
    "member_drug_name",
    "source_remark",
    "source_sheet",
    "source_file_version",
    "ingested_at",
)

BRAND_GROUP_MEMBERS = {
    "strategy_011": {
        "엔브렐": {"엔브렐", "엔브렐마이클릭"},
        "오렌시아": {"오렌시아", "오렌시아서브큐"},
        "젤잔즈": {"젤잔즈", "젤잔즈엑스알"},
    }
}

EXPECTED_BRAND_GROUP_COUNTS = expected_mapping("master_brand_consolidation.brand_group_counts")


@dataclass
class BrandConsolidationStats:
    strategic_market_id: str = STRATEGIC_MARKET_ID
    sheet_name: str = SOURCE_SHEET
    header_row: int = HEADER_ROW
    raw_rows_scanned: int = 0
    empty_rows: int = 0
    excluded_rows: int = 0
    staging_drug_rows: int = 0
    brand_consolidation_rows: int = 0
