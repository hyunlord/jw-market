from __future__ import annotations

import pytest

from pipeline.orchestrator.full_rehearsal_sidecars import prepare_malb_table


def test_prepare_malb_creates_isolated_table_from_reference_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Connection()
    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_sidecars.table_exists",
        lambda _conn, db_name, _table: db_name == "jw_mart_d2_stage_20260630_r2",
    )

    statement = prepare_malb_table(
        conn,
        reference_db="jw_mart_d2_stage_20260630_r2",
        target_db="jw_mart_rehearsal_r1_20260718",
    )

    assert statement == (
        "CREATE TABLE `jw_mart_rehearsal_r1_20260718`.`mart_analysis_level_block` "
        "LIKE `jw_mart_d2_stage_20260630_r2`.`mart_analysis_level_block`"
    )
    assert conn.statements == [statement]


@pytest.mark.parametrize(
    "target_db",
    ("jw_mart_d2_stage_20260630_r2", "jw_mart", "jw_mart_dim_stage_unsafe"),
)
def test_prepare_malb_rejects_non_rehearsal_targets(target_db: str) -> None:
    with pytest.raises(ValueError, match="jw_mart_rehearsal_"):
        prepare_malb_table(
            _Connection(),
            reference_db="jw_mart_d2_stage_20260630_r2",
            target_db=target_db,
        )


class _Cursor:
    def __init__(self, statements: list[str]) -> None:
        self._statements = statements

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str) -> None:
        self._statements.append(statement)


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.statements)
