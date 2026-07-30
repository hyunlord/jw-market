from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pymysql
import pytest

from pipeline.etl.io.mart.filter_dimension_copy import TableCopyProof
from pipeline.etl.io.mart.filter_dimension_promote import (
    promote_filter_dimension_rows,
)


@pytest.fixture(autouse=True)
def _verified_snapshot_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline.etl.io.mart import filter_dimension_swap

    monkeypatch.setattr(
        filter_dimension_swap,
        "copy_table_consistent_snapshot",
        lambda *_args, **_kwargs: TableCopyProof(3, "verified", "verified"),
    )


class _Cursor:
    def __init__(
        self,
        *,
        fail_backup_batch: int | None = None,
        fail_cache_delete: bool = False,
        rollback_fixture: bool = False,
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...] | None]] = []
        self.rows: list[dict[str, Any]] = []
        self.rowcount = 0
        self._backup_batch = 0
        self._backup_rows = 0
        self._stage_rows = 0
        self._fail_backup_batch = fail_backup_batch
        self._fail_cache_delete = fail_cache_delete
        self._rollback_fixture = rollback_fixture

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        bound = tuple(params) if params is not None else None
        self.calls.append((sql, bound))
        self.rowcount = 0
        if "AS table_count" in sql:
            table_name = str(bound[1]) if bound is not None else ""
            exists = self._rollback_fixture and "__failed_" not in table_name
            self.rows = [{"table_count": int(exists)}]
        elif "SELECT COUNT(*) AS n" in sql:
            if "computed_at=%s" in sql:
                count = 1
            elif "__stage_" in sql:
                count = self._stage_rows
            elif "__old_" in sql:
                count = self._backup_rows or 3
            else:
                count = 3
            self.rows = [{"n": count}]
        elif "INSERT INTO" in sql and "__old_" in sql and "SELECT *" in sql:
            self._backup_batch += 1
            if self._backup_batch == self._fail_backup_batch:
                raise pymysql.OperationalError(1180, "injected backup batch failure")
            inserted = min(2, 3 - self._backup_rows)
            self._backup_rows += inserted
            self.rowcount = inserted
        elif "INSERT INTO" in sql and "__stage_" in sql and "SELECT *" in sql:
            inserted = min(2, 3 - self._stage_rows)
            self._stage_rows += inserted
            self.rowcount = inserted
        elif "SELECT MAX(id) AS max_id" in sql:
            self.rows = [{"max_id": max(self._backup_rows, self._stage_rows)}]
        elif "DELETE FROM" in sql and "cache_dynamic_market_response" in sql:
            if self._fail_cache_delete:
                raise pymysql.OperationalError(1205, "injected cache delete failure")

    def executemany(
        self,
        sql: str,
        params: Sequence[Sequence[Any]],
    ) -> None:
        payloads = tuple(tuple(item) for item in params)
        self.calls.append((sql, payloads))
        self.rowcount = len(payloads)

    def fetchone(self) -> dict[str, Any]:
        return self.rows[0]

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _Connection:
    def __init__(
        self,
        *,
        fail_backup_batch: int | None = None,
        fail_cache_delete: bool = False,
        rollback_fixture: bool = False,
    ) -> None:
        self.cursor_instance = _Cursor(
            fail_backup_batch=fail_backup_batch,
            fail_cache_delete=fail_cache_delete,
            rollback_fixture=rollback_fixture,
        )
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _computed_rows() -> list[dict[str, Any]]:
    return [
        {
            "source": "ubist",
            "measure": "sales",
            "atc4_code": "C10C0",
            "brand_key": "brand-a",
            "brand_name": "Brand A",
            "product_code": "p1",
            "dimension_type": "molecule",
            "dimension_value": "A / B",
            "dimension_value_norm": "A / B",
            "raw_value_history": {"202601": 1},
        }
    ]


def test_forward_promotion_only_exposes_complete_table_at_atomic_rename() -> None:
    conn = _Connection()

    promote_filter_dimension_rows(
        conn,
        _computed_rows(),
        target_db="jw_mart_d2_stage_20260630_r2",
        snapshot_conn=object(),
        source="ubist",
        dimension_type="molecule",
        build_marker="2026-07-29 00:00:00",
        batch_size=2,
        allow_shared_serving_target=True,
        promotion_run_id="fdm_atomic",
    )

    statements = [sql for sql, _params in conn.cursor_instance.calls]
    rename_index = next(
        index for index, sql in enumerate(statements) if sql.startswith("RENAME TABLE")
    )
    live = "`jw_mart_d2_stage_20260630_r2`.`mart_general_filter_dimension_metric`"
    live_mutations = (
        f"INSERT INTO {live}",
        f"UPDATE {live}",
        f"DELETE FROM {live}",
    )
    assert all(
        not sql.lstrip().startswith(live_mutations)
        for sql in statements[:rename_index]
    )
    assert "__stage_fdm_atomic" in statements[rename_index]
    assert "__old_fdm_atomic" in statements[rename_index]


def test_forward_promotion_invalidates_only_ubist_dynamic_cache() -> None:
    conn = _Connection()

    promote_filter_dimension_rows(
        conn,
        _computed_rows(),
        target_db="jw_mart_d2_stage_20260630_r2",
        snapshot_conn=object(),
        source="ubist",
        dimension_type="molecule",
        build_marker="2026-07-29 00:00:00",
        batch_size=2,
        allow_shared_serving_target=True,
        promotion_run_id="fdm_cache",
    )

    invalidations = [
        (sql, params)
        for sql, params in conn.cursor_instance.calls
        if "cache_dynamic_market_response" in sql
    ]
    assert len(invalidations) == 1
    sql, params = invalidations[0]
    assert "DELETE FROM" in sql
    assert "JSON_EXTRACT(request_json, '$.source')" in sql
    assert params == ("ubist",)
    assert conn.commits > 0


def test_post_swap_failure_restores_previous_live_table() -> None:
    conn = _Connection(fail_cache_delete=True)

    with pytest.raises(RuntimeError, match="previous live table restored"):
        promote_filter_dimension_rows(
            conn,
            _computed_rows(),
            target_db="jw_mart_d2_stage_20260630_r2",
            snapshot_conn=object(),
            source="ubist",
            dimension_type="molecule",
            build_marker="2026-07-29 00:00:00",
            batch_size=2,
            allow_shared_serving_target=True,
            promotion_run_id="fdm_restore",
        )

    renames = [
        sql
        for sql, _params in conn.cursor_instance.calls
        if sql.startswith("RENAME TABLE")
    ]
    assert len(renames) == 2
    assert "__old_fdm_restore" in renames[0]
    assert "__old_fdm_restore" in renames[1]
    assert "__stage_fdm_restore" in renames[1]
    assert conn.rollbacks == 1


def test_activation_record_failure_restores_previous_live_table() -> None:
    conn = _Connection()

    def fail_record(_backup: dict[str, Any]) -> None:
        raise RuntimeError("injected activation record failure")

    with pytest.raises(RuntimeError, match="previous live table restored"):
        promote_filter_dimension_rows(
            conn,
            _computed_rows(),
            target_db="jw_mart_d2_stage_20260630_r2",
            snapshot_conn=object(),
            source="ubist",
            dimension_type="molecule",
            build_marker="2026-07-29 00:00:00",
            batch_size=2,
            allow_shared_serving_target=True,
            promotion_run_id="fdm_record_restore",
            on_activated=fail_record,
        )

    renames = [
        sql
        for sql, _params in conn.cursor_instance.calls
        if sql.startswith("RENAME TABLE")
    ]
    assert len(renames) == 2
    assert conn.rollbacks == 1


def test_standalone_fdm_rollback_restores_backup_without_generic_generation() -> None:
    from pipeline.etl.io.mart.filter_dimension_promote import (
        rollback_filter_dimension_promotion,
    )

    conn = _Connection(rollback_fixture=True)
    result = rollback_filter_dimension_promotion(
        conn,
        target_db="jw_mart_d2_stage_20260630_r2",
        promotion_run_id="fdm_rollback",
        expected_backup_rows=3,
    )

    statements = [sql for sql, _params in conn.cursor_instance.calls]
    rename = next(sql for sql in statements if sql.startswith("RENAME TABLE"))
    assert "__old_fdm_rollback" in rename
    assert "__failed_fdm_rollback" in rename
    assert result["restored_rows"] == 3
    assert any("cache_dynamic_market_response" in sql for sql in statements)
