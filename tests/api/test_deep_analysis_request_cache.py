from __future__ import annotations

from pipeline.scripts.api import db
from pipeline.scripts.api.deep_analysis_request_cache import request_cache_scope


def test_request_cache_reuses_identical_selects_only_inside_scope(monkeypatch) -> None:
    calls: list[str] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, _params):
            calls.append(sql)

        def fetchall(self):
            return [{"value": len(calls)}]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(db, "borrow_read_connection", lambda: Connection())

    with request_cache_scope() as cache:
        first = db.fetch_all("SELECT value FROM metric WHERE market_id=%s", ("ml_001",))
        second = db.fetch_all("SELECT value FROM metric WHERE market_id=%s", ("ml_001",))

    assert first == second
    assert len(calls) == 1
    assert cache.stats().query_hits == 1
    assert cache.stats().query_misses == 1


def test_request_cache_does_not_cross_request_boundaries(monkeypatch) -> None:
    calls: list[str] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, _params):
            calls.append(sql)

        def fetchall(self):
            return []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(db, "borrow_read_connection", lambda: Connection())

    with request_cache_scope():
        db.fetch_all("SELECT 1")
    with request_cache_scope():
        db.fetch_all("SELECT 1")

    assert len(calls) == 2


def test_disabled_request_cache_is_negative_control(monkeypatch) -> None:
    calls: list[str] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, _params):
            calls.append(sql)

        def fetchall(self):
            return []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(db, "borrow_read_connection", lambda: Connection())

    with request_cache_scope(enabled=False):
        db.fetch_all("SELECT 1")
        db.fetch_all("SELECT 1")

    assert len(calls) == 2
