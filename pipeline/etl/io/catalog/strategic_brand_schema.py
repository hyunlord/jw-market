from __future__ import annotations

import pyarrow as pa

EXPECTED_ROW_COUNT = 4495
EXPECTED_STAGING_ROWS = 3952
EXPECTED_EXCLUDED_ROWS = 543
EXPECTED_COLUMNS = (
    "brand_id",
    "name",
    "merge_name",
    "ml_id",
    "cd_id",
    "is_excluded",
    "is_class_excluded",
    "allowed_atc4_codes_json",
    "class",
    "class_1",
    "class_2",
    "molecule",
    "dosage_form",
    "strength_pack",
    "nhi_type",
    "ox_gx",
    "fish_oil",
    "판매사",
    "제조사",
    "source_file_version",
    "ingested_at",
)
EXPECTED_ML_COUNTS = {
    "ml_001": 358,
    "ml_002": 45,
    "ml_003": 82,
    "ml_004": 10,
    "ml_005": 294,
    "ml_006": 1095,
    "ml_007": 611,
    "ml_008": 1081,
    "ml_009": 406,
    "ml_010": 10,
    "ml_011": 26,
    "ml_012": 76,
    "ml_013": 14,
    "ml_014": 331,
    "ml_015": 4,
    "ml_016": 52,
}
PHASE12_CD_BASELINE = {
    "cd_001": 116,
    "cd_002": 24,
    "cd_003": 18,
    "cd_004": 10,
    "cd_005": 11,
    "cd_006": 1047,
    "cd_007": 117,
    "cd_008": 20,
    "cd_009": 26,
    "cd_010": 160,
    "cd_011": 140,
    "cd_012": 8,
    "cd_013": 2,
    "cd_014": 26,
    "cd_015": 16,
    "cd_016": 13,
    "cd_017": 4,
    "cd_018": 64,
    "cd_019": 8,
}
SHEET_TOTAL_FILTER_IDS = {"cdf_004", "cdf_006", "cdf_007", "cdf_014", "cdf_016", "cdf_017"}
MERGE_NAME_BY_NAME = {
    "엔브렐마이클릭": "엔브렐",
    "엔브렐": "엔브렐",
    "오렌시아": "오렌시아",
    "오렌시아서브큐": "오렌시아",
    "젤잔즈": "젤잔즈",
    "젤잔즈엑스알": "젤잔즈",
}

STRATEGIC_BRAND_SCHEMA = pa.schema(
    [
        pa.field("brand_id", pa.string(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        pa.field("merge_name", pa.string(), nullable=False),
        pa.field("ml_id", pa.string(), nullable=False),
        pa.field("cd_id", pa.string(), nullable=True),
        pa.field("is_excluded", pa.bool_(), nullable=False),
        pa.field("is_class_excluded", pa.bool_(), nullable=False),
        pa.field("allowed_atc4_codes_json", pa.string(), nullable=True),
        pa.field("class", pa.string(), nullable=True),
        pa.field("class_1", pa.string(), nullable=True),
        pa.field("class_2", pa.string(), nullable=True),
        pa.field("molecule", pa.string(), nullable=True),
        pa.field("dosage_form", pa.string(), nullable=True),
        pa.field("strength_pack", pa.string(), nullable=True),
        pa.field("nhi_type", pa.string(), nullable=True),
        pa.field("ox_gx", pa.string(), nullable=True),
        pa.field("fish_oil", pa.string(), nullable=True),
        pa.field("판매사", pa.string(), nullable=True),
        pa.field("제조사", pa.string(), nullable=True),
        pa.field("source_file_version", pa.string(), nullable=False),
        pa.field("ingested_at", pa.timestamp("us"), nullable=False),
    ]
)
