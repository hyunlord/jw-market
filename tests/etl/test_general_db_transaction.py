from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline.etl.io.mart import general_db
from pipeline.etl.io.mart.general_json import write_jsonl


class _FakeCursor:
    def __init__(self, *, fail_insert: bool) -> None:
        self.fail_insert = fail_insert
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.executemany_calls = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.execute_calls.append((sql, params))

    def executemany(self, _sql: str, _payloads: list[tuple[Any, ...]]) -> None:
        self.executemany_calls += 1
        if self.fail_insert:
            raise RuntimeError("injected insert failure")


class _FakeConnection:
    def __init__(self, *, fail_insert: bool = False) -> None:
        self.cursor_instance = _FakeCursor(fail_insert=fail_insert)
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
        [{"brand_key": "a", "source": "ubist"}],
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
        brand_path=brand_path,
        market_path=market_path,
        brand_columns=["brand_key", "source"],
        market_columns=["atc4_code", "source"],
    )

    delete_calls = connection.cursor_instance.execute_calls[:2]
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
            brand_path=brand_path,
            market_path=market_path,
            brand_columns=["brand_key", "source"],
            market_columns=["atc4_code", "source"],
            commit_each_batch=True,
        )

    assert connection.commit_calls == 1
    assert connection.rolled_back is True
    assert connection.closed is True
