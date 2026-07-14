from __future__ import annotations

from pathlib import Path

from pipeline.etl.stages.s4_mart import GENERAL_BRAND_DDL


ROOT = Path(__file__).resolve().parents[2]


def test_general_mart_schema_contains_brand_name_search_index() -> None:
    assert "INDEX idx_general_brand_name (brand_name, measure)" in GENERAL_BRAND_DDL


def test_online_index_migration_and_rollback_are_explicit() -> None:
    migration = (
        ROOT / "pipeline/scripts/deploy/sql/mart_general_brand_search_indexes.sql"
    ).read_text()
    rollback = (
        ROOT / "pipeline/scripts/deploy/sql/mart_general_brand_search_indexes_rollback.sql"
    ).read_text()

    assert "ADD INDEX idx_general_brand_name (brand_name, measure)" in migration
    assert "ALGORITHM=INPLACE" in migration and "LOCK=NONE" in migration
    assert "DROP INDEX idx_general_brand_name" in rollback
    assert "ALGORITHM=INPLACE" in rollback and "LOCK=NONE" in rollback
