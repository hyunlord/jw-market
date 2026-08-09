from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.etl.stages import s4_mart
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


class _IndexCursor:
    def __init__(self, index_row: dict[str, object] | None) -> None:
        self.index_row = index_row
        self.statements: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object = None) -> int:
        self.statements.append((sql, params))
        return 0

    def fetchone(self) -> dict[str, object] | None:
        return self.index_row


def test_build_schema_adds_missing_brand_name_index_before_copy() -> None:
    cursor = _IndexCursor(None)

    s4_mart._ensure_general_brand_search_index(cursor, "build_db")

    sql = "\n".join(statement for statement, _params in cursor.statements)
    assert (
        "ALTER TABLE `build_db`.`mart_general_brand_metric` "
        "ADD INDEX `idx_general_brand_name` (`brand_name`, `measure`)"
    ) in sql


def test_build_schema_rejects_wrong_brand_name_index_shape() -> None:
    cursor = _IndexCursor(
        {"non_unique": 1, "index_columns": "brand_name,source"}
    )

    with pytest.raises(RuntimeError, match="brand search index contract drift"):
        s4_mart._ensure_general_brand_search_index(cursor, "build_db")


def test_build_schema_keeps_matching_brand_name_index() -> None:
    cursor = _IndexCursor(
        {"non_unique": 1, "index_columns": "brand_name,measure"}
    )

    s4_mart._ensure_general_brand_search_index(cursor, "build_db")

    assert len(cursor.statements) == 1
    assert "information_schema.STATISTICS" in cursor.statements[0][0]
