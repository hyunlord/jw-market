from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline.etl.io.mart import general_db
from pipeline.etl.io.mart.general_json import write_jsonl


class _FakeCursor:
    def __init__(
        self,
        *,
        fail_insert: bool,
        existing_by_table: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.fail_insert = fail_insert
        self.existing_by_table = existing_by_table or {}
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.executemany_calls = 0
        self.executemany_payloads: list[list[tuple[Any, ...]]] = []
        self._selected: list[dict[str, Any]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.execute_calls.append((sql, params))
        if sql.startswith("SELECT "):
            table = next((
                name
                for name in self.existing_by_table
                if f"FROM {name} " in sql
            ), None)
            self._selected = self.existing_by_table.get(table, [])

    def fetchall(self) -> list[dict[str, Any]]:
        return self._selected

    def executemany(self, _sql: str, _payloads: list[tuple[Any, ...]]) -> None:
        self.executemany_calls += 1
        self.executemany_payloads.append(_payloads)
        if self.fail_insert:
            raise RuntimeError("injected insert failure")


class _FakeConnection:
    def __init__(
        self,
        *,
        fail_insert: bool = False,
        existing_by_table: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.cursor_instance = _FakeCursor(
            fail_insert=fail_insert,
            existing_by_table=existing_by_table,
        )
        self.autocommit_values: list[bool] = []
        self.commit_calls = 0
        self.rolled_back = False
        self.closed = False

    def autocommit(self, value: bool) -> None:
        self.autocommit_values.append(value)

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    brand_path = tmp_path / "brand.jsonl"
    market_path = tmp_path / "market.jsonl"
    write_jsonl(
        brand_path,
        [{"brand_key": "a", "atc4_code": "C10A1", "source": "ubist"}],
    )
    write_jsonl(
        market_path,
        [{"atc4_code": "C10A1", "source": "ubist"}],
    )
    return brand_path, market_path


def test_jsonl_source_replace_commits_both_tables_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = _FakeConnection()
    monkeypatch.setattr(general_db, "mariadb_connect", lambda: connection)
    brand_path, market_path = _paths(tmp_path)

    general_db.replace_source_rows_from_jsonl(
        source="ubist",
        brand_path=brand_path,
        market_path=market_path,
        brand_columns=["brand_key", "source"],
        market_columns=["atc4_code", "source"],
    )

    assert connection.autocommit_values == [False]
    assert len(connection.cursor_instance.execute_calls) == 2
    assert connection.cursor_instance.executemany_calls == 2
    assert connection.commit_calls == 1
    assert connection.rolled_back is False
    assert connection.closed is True


def test_jsonl_source_replace_rolls_back_injected_insert_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = _FakeConnection(fail_insert=True)
    monkeypatch.setattr(general_db, "mariadb_connect", lambda: connection)
    brand_path, market_path = _paths(tmp_path)

    with pytest.raises(RuntimeError, match="injected insert failure"):
        general_db.replace_source_rows_from_jsonl(
            source="ubist",
            brand_path=brand_path,
            market_path=market_path,
            brand_columns=["brand_key", "source"],
            market_columns=["atc4_code", "source"],
        )

    assert connection.commit_calls == 0
    assert connection.rolled_back is True
    assert connection.closed is True


def test_jsonl_scoped_replace_deletes_only_requested_atc4(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = _FakeConnection()
    monkeypatch.setattr(general_db, "mariadb_connect", lambda: connection)
    brand_path, market_path = _paths(tmp_path)

    general_db.replace_scoped_source_rows_from_jsonl(
        source="ubist",
        atc4_scope=("C10A1", "A10B2"),
        period_scope=("2026-05",),
        brand_path=brand_path,
        market_path=market_path,
        brand_columns=["brand_key", "source"],
        market_columns=["atc4_code", "source"],
    )

    delete_calls = [
        (sql, params)
        for sql, params in connection.cursor_instance.execute_calls
        if sql.startswith("DELETE FROM ")
    ]
    assert len(delete_calls) == 2
    assert all("source=%s AND atc4_code IN (%s,%s)" in sql for sql, _ in delete_calls)
    assert all(params == ("ubist", "A10B2", "C10A1") for _, params in delete_calls)
    assert connection.cursor_instance.executemany_calls == 2
    assert connection.commit_calls == 1


def test_jsonl_source_replace_commits_each_batch_for_isolated_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = _FakeConnection()
    monkeypatch.setattr(general_db, "mariadb_connect", lambda: connection)
    brand_path, market_path = _paths(tmp_path)

    general_db.replace_source_rows_from_jsonl(
        source="ubist",
        brand_path=brand_path,
        market_path=market_path,
        brand_columns=["brand_key", "source"],
        market_columns=["atc4_code", "source"],
        commit_each_batch=True,
    )

    assert connection.commit_calls == 2
    assert connection.rolled_back is False
    assert connection.closed is True


def test_jsonl_source_replace_bounds_isolated_source_deletes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = _FakeConnection()
    delete_results = iter((2, 1, 0, 1, 0))

    def execute(sql: str, params: tuple[object, ...]) -> int:
        connection.cursor_instance.execute_calls.append((sql, params))
        if sql.startswith("DELETE FROM "):
            return next(delete_results)
        return 0

    connection.cursor_instance.execute = execute  # type: ignore[method-assign]
    monkeypatch.setattr(general_db, "mariadb_connect", lambda: connection)
    brand_path, market_path = _paths(tmp_path)

    general_db.replace_source_rows_from_jsonl(
        source="ubist",
        brand_path=brand_path,
        market_path=market_path,
        brand_columns=["brand_key", "source"],
        market_columns=["atc4_code", "source"],
        batch_size=2,
        commit_each_batch=True,
    )

    delete_calls = [
        (sql, params)
        for sql, params in connection.cursor_instance.execute_calls
        if sql.startswith("DELETE FROM ")
    ]
    assert len(delete_calls) == 5
    assert all("ORDER BY id LIMIT %s" in sql for sql, _params in delete_calls)
    assert all(params == ("ubist", 2) for _sql, params in delete_calls)
    assert connection.commit_calls == 5


def test_jsonl_scoped_replace_bounds_isolated_deletes_for_gcache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = _FakeConnection()
    delete_results = iter((2, 1, 0, 1, 0))

    def execute(sql: str, params: tuple[object, ...]) -> int:
        connection.cursor_instance.execute_calls.append((sql, params))
        if sql.startswith("DELETE FROM "):
            return next(delete_results)
        return 0

    connection.cursor_instance.execute = execute  # type: ignore[method-assign]
    monkeypatch.setattr(general_db, "mariadb_connect", lambda: connection)
    brand_path, market_path = _paths(tmp_path)

    general_db.replace_scoped_source_rows_from_jsonl(
        source="ubist",
        atc4_scope=("C10A1", "A10B2"),
        period_scope=("2026-05",),
        brand_path=brand_path,
        market_path=market_path,
        brand_columns=["brand_key", "source"],
        market_columns=["atc4_code", "source"],
        commit_each_batch=True,
    )

    delete_calls = [
        (sql, params)
        for sql, params in connection.cursor_instance.execute_calls
        if sql.startswith("DELETE FROM ")
    ]
    assert len(delete_calls) == 5
    assert all(
        "source=%s AND atc4_code IN (%s,%s) ORDER BY id LIMIT %s" in sql
        for sql, _params in delete_calls
    )
    assert all(
        params == ("ubist", "A10B2", "C10A1", 100)
        for _sql, params in delete_calls
    )
    assert connection.commit_calls == 5


def test_jsonl_scoped_replace_rolls_back_current_batch_after_partial_commits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = _FakeConnection(fail_insert=True)
    delete_results = iter((1, 0, 0))

    def execute(sql: str, params: tuple[object, ...]) -> int:
        connection.cursor_instance.execute_calls.append((sql, params))
        if sql.startswith("DELETE FROM "):
            return next(delete_results)
        return 0

    connection.cursor_instance.execute = execute  # type: ignore[method-assign]
    monkeypatch.setattr(general_db, "mariadb_connect", lambda: connection)
    brand_path, market_path = _paths(tmp_path)

    with pytest.raises(RuntimeError, match="injected insert failure"):
        general_db.replace_scoped_source_rows_from_jsonl(
            source="ubist",
            atc4_scope=("C10A1",),
            period_scope=("2026-05",),
            brand_path=brand_path,
            market_path=market_path,
            brand_columns=["brand_key", "source"],
            market_columns=["atc4_code", "source"],
            commit_each_batch=True,
        )

    assert connection.commit_calls == 1
    assert connection.rolled_back is True
    assert connection.closed is True


def test_scoped_replace_requires_explicit_period_scope(tmp_path: Path) -> None:
    brand_path, market_path = _paths(tmp_path)

    with pytest.raises(ValueError, match="period scope"):
        general_db.replace_scoped_source_rows_from_jsonl(
            source="ubist",
            atc4_scope=("C10A1",),
            period_scope=(),
            brand_path=brand_path,
            market_path=market_path,
            brand_columns=["brand_key", "source"],
            market_columns=["atc4_code", "source"],
        )


def test_scoped_replace_reads_existing_row_and_upserts_period_merged_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    brand_path = tmp_path / "brand.jsonl"
    market_path = tmp_path / "market.jsonl"
    write_jsonl(
        brand_path,
        [
            {
                "brand_key": "a",
                "atc4_code": "C10A1",
                "source": "ubist",
                "measure": "sales",
                "metric_history": {
                    "2026-04": {"raw_value": 999},
                    "2026-05": {"raw_value": 20},
                },
                "raw_value_history": {"2026-04": 999, "2026-05": 20},
                "payload": {},
            }
        ],
    )
    write_jsonl(
        market_path,
        [
            {
                "atc4_code": "C10A1",
                "source": "ubist",
                "measure": "sales",
                "market_size_series": {"2026-04": 999, "2026-05": 20},
                "payload": {},
            }
        ],
    )
    connection = _FakeConnection(
        existing_by_table={
            "mart_general_brand_metric": [
                {
                    "brand_key": "a",
                    "atc4_code": "C10A1",
                    "source": "ubist",
                    "measure": "sales",
                    "metric_history": '{"2026-04":{"raw_value":10},"2026-05":{"raw_value":11}}',
                    "raw_value_history": '{"2026-04":10,"2026-05":11}',
                    "payload": "{}",
                }
            ],
            "mart_general_market_metric": [
                {
                    "atc4_code": "C10A1",
                    "source": "ubist",
                    "measure": "sales",
                    "market_size_series": '{"2026-04":10,"2026-05":11}',
                    "payload": "{}",
                }
            ],
        }
    )
    monkeypatch.setattr(general_db, "mariadb_connect", lambda: connection)

    general_db.replace_scoped_source_rows_from_jsonl(
        source="ubist",
        atc4_scope=("C10A1",),
        period_scope=("2026-05",),
        brand_path=brand_path,
        market_path=market_path,
        brand_columns=[
            "brand_key",
            "atc4_code",
            "source",
            "measure",
            "metric_history",
            "raw_value_history",
            "payload",
        ],
        market_columns=[
            "atc4_code",
            "source",
            "measure",
            "market_size_series",
            "payload",
        ],
    )

    brand_payload = connection.cursor_instance.executemany_payloads[0][0]
    market_payload = connection.cursor_instance.executemany_payloads[1][0]
    assert brand_payload[4] == '{"2026-04":{"raw_value":10},"2026-05":{"raw_value":20}}'
    assert brand_payload[5] == '{"2026-04":10,"2026-05":20}'
    assert market_payload[3] == '{"2026-04":10,"2026-05":20}'
