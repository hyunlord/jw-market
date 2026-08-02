from __future__ import annotations

import os
from pathlib import Path

from pipeline.etl.io.catalog.paths import CATALOG_ROOT_ENV

from .general_config import PROJECT_ROOT, first_existing

DRY_RUN_DIR = Path("/tmp")
ML_BRAND_JSONL = "strategic_ml_v3_brand_rows.jsonl"
ML_MARKET_JSONL = "strategic_ml_v3_market_rows.jsonl"
CD_BRAND_JSONL = "strategic_cd_v3_brand_rows.jsonl"
CD_MARKET_JSONL = "strategic_cd_v3_market_rows.jsonl"
UBIST_MEASURES = ("sales", "volume")
IQVIA_MEASURES = ("sales", "unit", "dosage_unit", "counting_unit")
OVERRIDE_COLS = ["class", "class_1", "class_2", "molecule", "dosage_form", "strength_pack", "nhi_type", "ox_gx", "fish_oil", "판매사", "제조사"]


def catalog_dir() -> Path:
    configured = os.environ.get("S5_CATALOG_DIR") or os.environ.get(CATALOG_ROOT_ENV)
    root = Path(configured) if configured else first_existing(
        PROJECT_ROOT / "output" / "catalog",
        PROJECT_ROOT / "parquet",
    )
    if not root.is_dir():
        raise FileNotFoundError(f"S5 catalog root not found: {root}")
    return root


def catalog_file(name: str) -> Path:
    path = catalog_dir() / name / f"{name}.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"S5 catalog artifact not found: {path}")
    return path

ML_BRAND_COLUMNS = [
    "ml_id", "brand_id", "brand_key", "brand_name", "source", "measure", "is_jw", "unit_label",
    "metric_history", "extended_metric_history", "channel_data", "specialty_data", "dimension_data",
    "dimension_channel_data", "dimension_specialty_data", "by_dimension", "raw_value_history",
    "ubist_channel_by_display", "ubist_channel_by_code", "overlay_data", "payload",
]
ML_MARKET_COLUMNS = [
    "ml_id", "ml_name", "source", "measure", "unit_label", "market_size_series", "hhi_series_5y",
    "brand_ranking_stacked", "company_ranking_stacked", "company_concentration_trend", "ei_ms_matrix",
    "growth_contribution_ms_matrix", "growth_contribution", "analysis_levels", "level_top5_trend",
    "target_customer_competition", "payload",
]
CD_BRAND_COLUMNS = [
    "cd_market_id", "cd_brand_id", "brand_key", "brand_name", "source", "measure", "is_jw",
    "unit_label", "metric_history", "extended_metric_history", "channel_data", "specialty_data",
    "dimension_data", "dimension_channel_data", "by_dimension", "raw_value_history",
    "ubist_channel_by_display", "ubist_channel_by_code", "cd_overlay", "overlay_data", "payload",
]
CD_MARKET_COLUMNS = [
    "cd_market_id", "cd_market_name", "source", "measure", "unit_label", "market_size_series",
    "hhi_series_5y", "brand_ranking_stacked", "company_ranking_stacked", "company_concentration_trend",
    "ei_ms_matrix", "growth_contribution_ms_matrix", "growth_contribution", "analysis_levels",
    "level_top5_trend", "target_customer_competition", "payload",
]
