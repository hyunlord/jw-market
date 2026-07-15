from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Thread
from typing import Any

import pymysql

from pipeline.scripts.api import db
from pipeline.scripts.api import deep_analysis_runtime


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._position = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _sql: str, _params: object) -> int:
        return len(self._rows)

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def fetchmany(self, size: int) -> list[dict[str, Any]]:
        rows = self._rows[self._position : self._position + size]
        self._position += len(rows)
        return list(rows)


class _FakeConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.autocommit_values: list[bool] = []
        self.closed = False
        self.ping_calls = 0

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def autocommit(self, value: bool) -> None:
        self.autocommit_values.append(value)

    def cursor(self, _cursorclass=None) -> _FakeCursor:
        return _FakeCursor(self._rows)

    def ping(self, *, reconnect: bool) -> None:
        assert reconnect is False
        self.ping_calls += 1

    def close(self) -> None:
        self.closed = True


def test_initialized_pool_reuses_one_physical_connection_for_sequential_reads(monkeypatch) -> None:
    rows = [{"brand_key": "guardlet", "rank": 1}]
    created: list[_FakeConnection] = []

    def fake_connect() -> _FakeConnection:
        connection = _FakeConnection(rows)
        created.append(connection)
        return connection

    monkeypatch.setattr(db, "connect", fake_connect)
    db.init_pool(max_size=2)
    try:
        results = [db.fetch_all("SELECT brand_key, rank FROM mart") for _ in range(20)]

        assert results == [rows] * 20
        assert len(created) == 1
        assert created[0].autocommit_values == [True]
        assert created[0].closed is False
    finally:
        db.close_pool()

    assert created[0].closed is True


def test_iter_rows_reuses_initialized_pool_connection(monkeypatch) -> None:
    rows = [{"brand_key": "guardlet", "rank": 1}, {"brand_key": "mounjaro", "rank": 2}]
    created: list[_FakeConnection] = []

    def fake_connect() -> _FakeConnection:
        connection = _FakeConnection(rows)
        created.append(connection)
        return connection

    monkeypatch.setattr(db, "connect", fake_connect)
    db.init_pool(max_size=1)
    try:
        first = list(db.iter_rows("SELECT brand_key, rank FROM mart", batch_size=1))
        second = list(db.iter_rows("SELECT brand_key, rank FROM mart", batch_size=1))

        assert first == rows
        assert second == rows
        assert len(created) == 1
        assert db.pool_stats().connections_created == 1
    finally:
        db.close_pool()


def test_pool_bounds_concurrent_physical_connections(monkeypatch) -> None:
    created: list[_FakeConnection] = []
    barrier = Barrier(2)

    def fake_connect() -> _FakeConnection:
        connection = _FakeConnection([])
        created.append(connection)
        return connection

    def borrow_once() -> int:
        with db.borrow_read_connection() as connection:
            barrier.wait(timeout=2)
            return id(connection)

    monkeypatch.setattr(db, "connect", fake_connect)
    db.init_pool(max_size=2)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            connection_ids = set(executor.map(lambda _index: borrow_once(), range(2)))
        stats = db.pool_stats()

        assert len(connection_ids) == 2
        assert len(created) == 2
        assert stats.physical_connections == 2
        assert stats.idle_connections == 2
        assert stats.borrowed_connections == 0
        assert stats.connections_created == 2
    finally:
        db.close_pool()


def test_pool_shutdown_wakes_a_waiting_borrower(monkeypatch) -> None:
    created: list[_FakeConnection] = []
    waiting = Event()
    outcome: list[str] = []

    def fake_connect() -> _FakeConnection:
        connection = _FakeConnection([])
        created.append(connection)
        return connection

    def wait_for_connection() -> None:
        waiting.set()
        try:
            with db.borrow_read_connection():
                outcome.append("borrowed")
        except RuntimeError as exc:
            outcome.append(str(exc))

    monkeypatch.setattr(db, "connect", fake_connect)
    db.init_pool(max_size=1)
    held = db.borrow_read_connection()
    held.__enter__()
    waiter = Thread(target=wait_for_connection, daemon=True)
    waiter.start()
    assert waiting.wait(timeout=1)

    db.close_pool()
    held.__exit__(None, None, None)
    waiter.join(timeout=1)

    assert waiter.is_alive() is False
    assert outcome == ["database read pool is closed"]
    assert created[0].closed is True


def test_pool_returns_connection_after_non_connection_error(monkeypatch) -> None:
    created: list[_FakeConnection] = []

    def fake_connect() -> _FakeConnection:
        connection = _FakeConnection([])
        created.append(connection)
        return connection

    monkeypatch.setattr(db, "connect", fake_connect)
    db.init_pool(max_size=1)
    try:
        try:
            with db.borrow_read_connection():
                raise ValueError("consumer parsing failed")
        except ValueError:
            pass

        with db.borrow_read_connection() as reused:
            assert reused is created[0]
        assert db.pool_stats().borrowed_connections == 0
    finally:
        db.close_pool()


def test_pool_discards_connection_after_mysql_error(monkeypatch) -> None:
    created: list[_FakeConnection] = []

    def fake_connect() -> _FakeConnection:
        connection = _FakeConnection([])
        created.append(connection)
        return connection

    monkeypatch.setattr(db, "connect", fake_connect)
    db.init_pool(max_size=1)
    try:
        try:
            with db.borrow_read_connection():
                raise pymysql.ProgrammingError(1064, "invalid test query")
        except pymysql.ProgrammingError:
            pass

        with db.borrow_read_connection() as replacement:
            assert replacement is created[1]
        assert created[0].closed is True
        assert db.pool_stats().connections_created == 2
    finally:
        db.close_pool()


def test_pool_pings_only_connections_past_recycle_boundary(monkeypatch) -> None:
    created: list[_FakeConnection] = []

    def fake_connect() -> _FakeConnection:
        connection = _FakeConnection([])
        created.append(connection)
        return connection

    monkeypatch.setattr(db, "connect", fake_connect)
    db.init_pool(max_size=1, recycle_seconds=0)
    try:
        db.fetch_all("SELECT 1")
        db.fetch_all("SELECT 1")

        assert len(created) == 1
        assert created[0].ping_calls == 1
    finally:
        db.close_pool()


def test_deep_analysis_events_use_the_read_pool_without_changing_payload(monkeypatch) -> None:
    created: list[_FakeConnection] = []
    expected = {"cut_a": [{"title": "event"}], "cut_b": []}

    def fake_connect() -> _FakeConnection:
        connection = _FakeConnection([])
        created.append(connection)
        return connection

    monkeypatch.setattr(db, "connect", fake_connect)
    monkeypatch.setattr(
        deep_analysis_runtime,
        "build_events_for_cache",
        lambda _connection, _brand: expected,
    )
    db.init_pool(max_size=2)
    try:
        payloads = [deep_analysis_runtime._event_payload("가드렛") for _ in range(20)]

        assert payloads == [expected] * 20
        assert len(created) == 1
        assert db.pool_stats().connections_created == 1
    finally:
        db.close_pool()


def test_uninitialized_pool_preserves_standalone_connection_lifecycle(monkeypatch) -> None:
    rows = [{"brand_key": "mounjaro"}]
    created: list[_FakeConnection] = []

    def fake_connect() -> _FakeConnection:
        connection = _FakeConnection(rows)
        created.append(connection)
        return connection

    db.close_pool()
    monkeypatch.setattr(db, "connect", fake_connect)

    results = [db.fetch_all("SELECT brand_key FROM mart") for _ in range(20)]

    assert results == [rows] * 20
    assert len(created) == 20
    assert all(connection.autocommit_values == [] for connection in created)
    assert all(connection.closed is True for connection in created)
