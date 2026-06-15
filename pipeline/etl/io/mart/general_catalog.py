from __future__ import annotations

from typing import Any

import duckdb
import pandas as pd

from .brand_key_normalize import best_name, normalize_brand_name
from .general_config import CATALOG_DIR
from .general_utils import extract_atc4, normalize_period_label, normalise_iqvia_channel

def load_catalog_key_map() -> dict[str, dict[str, Any]]:
    catalog = pd.read_parquet(CATALOG_DIR / "strategic_brand" / "strategic_brand.parquet")
    mapping: dict[str, dict[str, Any]] = {}
    for _, row in catalog.iterrows():
        for col in ("name", "merge_name"):
            key = normalize_brand_name(row.get(col))
            if key and key not in mapping:
                mapping[key] = row.to_dict()
    return mapping

def _first_atc_code(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return extract_atc4(value)[0]
    if isinstance(parsed, list) and parsed:
        return str(parsed[0])
    return None

def _catalog_product_bridge() -> pd.DataFrame:
    products = pd.read_parquet(CATALOG_DIR / "strategic_product" / "strategic_product.parquet")
    brands = pd.read_parquet(CATALOG_DIR / "strategic_brand" / "strategic_brand.parquet")
    brand_cols = [
        col
        for col in (
            "brand_id",
            "name",
            "merge_name",
            "canonical_name",
            "general_brand_key",
            "allowed_atc4_codes_json",
            "판매사",
            "제조사",
        )
        if col in brands.columns
    ]
    merged = products.merge(brands[brand_cols].drop_duplicates("brand_id"), on="brand_id", how="left", suffixes=("_product", "_brand"))
    merged["catalog_atc4_code"] = merged.get("allowed_atc4_codes_json", "").map(_first_atc_code)
    merged["catalog_brand_name"] = merged.apply(
        lambda row: best_name(
            row.get("canonical_name"),
            row.get("general_brand_key"),
            row.get("merge_name_brand"),
            row.get("name_brand"),
            row.get("merge_name_product"),
            row.get("name_product"),
        ),
        axis=1,
    )
    merged["catalog_brand_key"] = merged["catalog_brand_name"].map(normalize_brand_name)
    merged["catalog_product_name"] = merged.apply(lambda row: best_name(row.get("name_product"), row.get("merge_name_product"), row.get("product_id")), axis=1)
    return merged.drop_duplicates("product_id")

def _attach_catalog(frame: pd.DataFrame) -> pd.DataFrame:
    bridge = _catalog_product_bridge()
    keep = [
        "product_id",
        "catalog_product_name",
        "catalog_brand_name",
        "catalog_brand_key",
        "catalog_atc4_code",
        "brand_id",
        "class",
        "molecule",
        "dosage_form",
        "strength_pack",
        "nhi_type",
        "ox_gx",
        "fish_oil",
        "판매사_product",
        "제조사_product",
        "판매사_brand",
        "제조사_brand",
    ]
    out = frame.merge(bridge[[col for col in keep if col in bridge.columns]], on="product_id", how="left")
    out["product_name"] = out.apply(lambda row: best_name(row.get("catalog_product_name"), row.get("product_id")), axis=1)
    out["brand_name"] = out.apply(lambda row: best_name(row.get("catalog_brand_name"), row.get("product_name"), row.get("product_id")), axis=1)
    out["brand_key"] = out.apply(
        lambda row: best_name(row.get("catalog_brand_key"), normalize_brand_name(row.get("brand_name"))),
        axis=1,
    )
    out["manufacturer"] = out.apply(lambda row: best_name(row.get("제조사_product"), row.get("제조사_brand")), axis=1)
    out["company"] = out.apply(lambda row: best_name(row.get("판매사_product"), row.get("판매사_brand")), axis=1)
    return out
