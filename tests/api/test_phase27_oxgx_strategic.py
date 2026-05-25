from __future__ import annotations

import subprocess

import pandas as pd

from pipeline.scripts.validation.phase27_oxgx_strategic_pipeline import (
    SOURCE_DERIVED_OX_GX_MARKETS,
    validate_catalog_ox_gx,
    validate_ubist_parquet_generic,
)


def test_ubist_latest_parquet_preserves_generic_values() -> None:
    issues = validate_ubist_parquet_generic()
    assert issues == []


def test_catalog_ox_gx_loaded_for_source_derived_markets_and_ml011_preserved() -> None:
    issues = validate_catalog_ox_gx()
    assert issues == []


def test_source_derived_ox_gx_markets_are_enabled_and_non_null() -> None:
    ml_market = pd.read_parquet("output/catalog/ml_market/ml_market.parquet")
    strategic_brand = pd.read_parquet("output/catalog/strategic_brand/strategic_brand.parquet")

    enabled = ml_market.set_index("ml_id")["analyze_ox_gx"].to_dict()
    for ml_id in SOURCE_DERIVED_OX_GX_MARKETS:
        assert bool(enabled[ml_id]) is True
        market_rows = strategic_brand[strategic_brand["ml_id"] == ml_id]
        assert set(market_rows["ox_gx"].dropna()) == {"Ox", "Gx"}
        assert market_rows["ox_gx"].isna().sum() == 0


def test_phase26_mart_rank_ms_validation_has_zero_issues_after_recompute() -> None:
    result = subprocess.run(
        ["python3", "pipeline/scripts/validation/phase26_mart_loading_pipeline.py"],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_phase27_pipeline_passes_without_api_gate() -> None:
    result = subprocess.run(
        ["python3", "pipeline/scripts/validation/phase27_oxgx_strategic_pipeline.py", "--skip-api"],
        capture_output=True,
        text=True,
        timeout=1200,
    )
    assert result.returncode == 0, result.stdout + result.stderr
