"""Regression checks for stale JSONL and silent-skip failure modes."""

from __future__ import annotations

from pathlib import Path


ETL_FILES = (
    Path("pipeline/scripts/etl/layer3_compute_strategic_ml_v3.py"),
    Path("pipeline/scripts/etl/layer3_compute_strategic_cd_v3.py"),
)


def test_strategic_etl_uses_db_first_general_rows() -> None:
    for path in ETL_FILES:
        source = path.read_text()
        assert "rows if rows else fetch_general_rows_from_db" not in source
        assert "fetch_general_rows_from_db(source)" in source


def test_strategic_etl_has_explicit_completeness_guards() -> None:
    ml_source = ETL_FILES[0].read_text()
    cd_source = ETL_FILES[1].read_text()

    assert "validate_market_completeness(ml_row, catalog_rows, selected)" in ml_source
    assert "validate_market_completeness(cd_row, catalog_rows, selected)" in cd_source
    assert "expected_measure_pairs" in ml_source
    assert "expected_measure_pairs" in cd_source
