from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from pipeline.etl.io.enrich.normalize import (
    clean_scalar,
    extract_bracket_code,
    normalize_brand,
    normalize_product_title,
)
from pipeline.etl.lib.ops_utils import find_project_root, first_existing


REPO_ROOT = find_project_root(Path(__file__).resolve())
CONFIG_DIR = REPO_ROOT / "pipeline" / "etl" / "config"
CATALOG_OUTPUT_DIR = first_existing(REPO_ROOT / "output" / "catalog", REPO_ROOT / "parquet")


def load_market_metadata(path: Path | None = None) -> dict[str, Any]:
    metadata_path = path or (CONFIG_DIR / "market_metadata.yaml")
    if not metadata_path.exists():
        raise FileNotFoundError(f"Missing market metadata: {metadata_path}")
    with metadata_path.open(encoding="utf-8") as fp:
        return yaml.safe_load(fp) or {}


def load_ml_market(catalog_root: Path = CATALOG_OUTPUT_DIR) -> pd.DataFrame:
    path = catalog_root / "ml_market" / "ml_market.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Missing ml_market parquet: {path}")
    return pd.read_parquet(path)


def load_strategic_product(ml_id: str, catalog_root: Path = CATALOG_OUTPUT_DIR) -> pd.DataFrame:
    path = catalog_root / "strategic_product" / "strategic_product.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Missing strategic_product parquet: {path}")
    sp = pd.read_parquet(path)
    products = sp[sp["ml_id"] == ml_id].copy()
    products["ubist_product_title"] = products["name"].fillna(products["merge_name"]).fillna("")
    products["iqvia_product_title"] = products["merge_name"].fillna(products["name"]).fillna("")
    products["ubist_product_key"] = products["ubist_product_title"].map(normalize_product_title)
    products["iqvia_product_key"] = products["iqvia_product_title"].map(normalize_product_title)
    products["product_key"] = products["ubist_product_key"]
    products["brand_key"] = products["iqvia_product_title"].map(normalize_brand)
    products["strength_bracket_code"] = products["strength_pack"].map(extract_bracket_code)
    products = products[(products["ubist_product_key"] != "") | (products["iqvia_product_key"] != "")].copy()
    return products


def ml_data_source(ml_row: pd.Series) -> str:
    value = clean_scalar(ml_row.get("data_source")).lower()
    if value in {"ubist", "iqvia", "both"}:
        return value
    return "iqvia"


def all_ml_ids(catalog_root: Path = CATALOG_OUTPUT_DIR) -> list[str]:
    ml = load_ml_market(catalog_root)
    return sorted(ml["ml_id"].tolist())
