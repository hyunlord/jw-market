from __future__ import annotations

import pyarrow as pa

EXPECTED_COLUMNS = (
    "product_id",
    "name",
    "merge_name",
    "brand_id",
    "ml_id",
    "cd_id",
    "class",
    "molecule",
    "molecule_raw",
    "dosage_form",
    "dosage_form_raw",
    "strength_pack",
    "nhi_type",
    "ox_gx",
    "fish_oil",
    "판매사",
    "제조사",
    "source_file_version",
    "ingested_at",
)

STRATEGIC_PRODUCT_SCHEMA = pa.schema(
    [
        pa.field("product_id", pa.string(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        pa.field("merge_name", pa.string(), nullable=False),
        pa.field("brand_id", pa.string(), nullable=False),
        pa.field("ml_id", pa.string(), nullable=False),
        pa.field("cd_id", pa.string(), nullable=True),
        pa.field("class", pa.string(), nullable=True),
        pa.field("molecule", pa.string(), nullable=True),
        pa.field("molecule_raw", pa.string(), nullable=True),
        pa.field("dosage_form", pa.string(), nullable=True),
        pa.field("dosage_form_raw", pa.string(), nullable=True),
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

UBIST_JOIN_KEY_BY_SMID = {
    "strategy_001": "ubist_brand_manufacturer",
    "strategy_005": "ubist_brand_manufacturer",
    "strategy_006": "ubist_product_manufacturer",
    "strategy_007": "ubist_product_manufacturer",
    "strategy_008": "ubist_brand_manufacturer",
    "strategy_009": "ubist_brand_manufacturer",
    "strategy_015": "ubist_brand_manufacturer",
}

IQVIA_JOIN_KEY_BY_SMID = {
    "strategy_002": "iqvia_atc4_molecule",
    "strategy_003": "iqvia_atc4_molecule",
    "strategy_004": "iqvia_manufacturer_atc4_molecule",
    "strategy_010": "iqvia_manufacturer_atc4_molecule",
    "strategy_011": "iqvia_manufacturer_atc4_molecule",
    "strategy_012": "iqvia_atc4_molecule",
    "strategy_013": "iqvia_atc4_molecule",
    "strategy_014": "iqvia_atc4_molecule",
    "strategy_015": "iqvia_pack_manufacturer_atc4",
    "strategy_016": "iqvia_manufacturer_atc4_molecule",
}
