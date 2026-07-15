from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import logging
import os
import re
from threading import Condition, Lock
from time import monotonic, perf_counter
from typing import Any

import pymysql

from pipeline.scripts.api.config import get_settings


logger = logging.getLogger(__name__)
_FROM_TABLE_RE = re.compile(r"\bFROM\s+([`\w.]+)", re.IGNORECASE)


def _stage_timing_enabled() -> bool:
    return os.getenv("LATENCY_STAGE_TIMING", "").strip().lower() in {"1", "true", "yes"}


def _sql_table(sql: str) -> str:
    match = _FROM_TABLE_RE.search(sql)
    return match.group(1) if match else "unknown"


@dataclass(frozen=True)
class PoolStats:
    enabled: bool
    max_size: int
    physical_connections: int
    idle_connections: int
    borrowed_connections: int
    connections_created: int


@dataclass(frozen=True)
class _IdleConnection:
    connection: pymysql.connections.Connection
    returned_at: float


class _ReadConnectionPool:
    def __init__(self, max_size: int, recycle_seconds: float) -> None:
        if max_size < 1:
            raise ValueError("DB_POOL_SIZE must be at least 1")
        if recycle_seconds < 0:
            raise ValueError("DB_POOL_RECYCLE_SECONDS cannot be negative")
        self.max_size = max_size
        self.recycle_seconds = recycle_seconds
        self._idle: list[_IdleConnection] = []
        self._condition = Condition()
        self._physical_connections = 0
        self._connections_created = 0
        self._closed = False

    def acquire(self) -> pymysql.connections.Connection:
        while True:
            with self._condition:
                if self._closed:
                    raise RuntimeError("database read pool is closed")
                if self._idle:
                    idle = self._idle.pop()
                    create = False
                elif self._physical_connections < self.max_size:
                    self._physical_connections += 1
                    idle = None
                    create = True
                else:
                    self._condition.wait()
                    continue

            if create:
                break
            assert idle is not None
            connection = self._reuse(idle)
            if connection is not None:
                with self._condition:
                    closed = self._closed
                if closed:
                    self._discard(connection)
                    raise RuntimeError("database read pool is closed")
                return connection

        try:
            connection = connect()
            connection.autocommit(True)
        except BaseException:
            with self._condition:
                self._physical_connections -= 1
                self._condition.notify()
            raise
        with self._condition:
            if self._closed:
                closed = True
            else:
                self._connections_created += 1
                closed = False
        if closed:
            self._discard(connection)
            raise RuntimeError("database read pool is closed")
        return connection

    def _reuse(self, idle: _IdleConnection) -> pymysql.connections.Connection | None:
        if monotonic() - idle.returned_at < self.recycle_seconds:
            return idle.connection
        try:
            idle.connection.ping(reconnect=False)
        except pymysql.MySQLError:
            self._discard(idle.connection)
            return None
        return idle.connection

    def release(self, connection: pymysql.connections.Connection, *, discard: bool = False) -> None:
        with self._condition:
            if not discard and not self._closed:
                self._idle.append(_IdleConnection(connection, monotonic()))
                self._condition.notify()
                return
        self._discard(connection)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            idle_connections = self._idle
            self._idle = []
            self._condition.notify_all()
        for idle in idle_connections:
            self._discard(idle.connection)

    def stats(self) -> PoolStats:
        with self._condition:
            physical = self._physical_connections
            created = self._connections_created
            idle = len(self._idle)
        return PoolStats(
            enabled=True,
            max_size=self.max_size,
            physical_connections=physical,
            idle_connections=idle,
            borrowed_connections=max(physical - idle, 0),
            connections_created=created,
        )

    def _discard(self, connection: pymysql.connections.Connection) -> None:
        try:
            connection.close()
        finally:
            with self._condition:
                self._physical_connections -= 1
                self._condition.notify()


_pool: _ReadConnectionPool | None = None
_pool_lock = Lock()


def init_pool(*, max_size: int | None = None, recycle_seconds: float | None = None) -> None:
    global _pool
    size = max_size if max_size is not None else int(os.getenv("DB_POOL_SIZE", "5"))
    recycle = (
        recycle_seconds
        if recycle_seconds is not None
        else float(os.getenv("DB_POOL_RECYCLE_SECONDS", "300"))
    )
    replacement = _ReadConnectionPool(size, recycle)
    with _pool_lock:
        previous, _pool = _pool, replacement
    if previous is not None:
        previous.close()


def close_pool() -> None:
    global _pool
    with _pool_lock:
        previous, _pool = _pool, None
    if previous is not None:
        previous.close()


def pool_stats() -> PoolStats:
    pool = _pool
    if pool is None:
        return PoolStats(False, 0, 0, 0, 0, 0)
    return pool.stats()


def connect() -> pymysql.connections.Connection:
    settings = get_settings()
    return pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


@contextmanager
def borrow_read_connection() -> Iterator[pymysql.connections.Connection]:
    """Borrow a connection for independent reads; transaction/session callers use connect()."""
    pool = _pool
    if pool is None:
        with connect() as connection:
            yield connection
        return

    connection = pool.acquire()
    discard = False
    try:
        yield connection
    except pymysql.MySQLError:
        discard = True
        raise
    finally:
        pool.release(connection, discard=discard)


def fetch_all(sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    started = perf_counter() if _stage_timing_enabled() else None
    with borrow_read_connection() as conn:
        with conn.cursor() as cur:
            execute_started = perf_counter() if started is not None else None
            cur.execute(sql, params or ())
            execute_ms = (perf_counter() - execute_started) * 1000 if execute_started is not None else None
            fetch_started = perf_counter() if started is not None else None
            rows = list(cur.fetchall())
            fetch_ms = (perf_counter() - fetch_started) * 1000 if fetch_started is not None else None
    if started is not None:
        logger.info(
            "market_latency_db op=fetch_all table=%s execute_ms=%.3f fetch_ms=%.3f rows=%d total_ms=%.3f",
            _sql_table(sql),
            execute_ms or 0.0,
            fetch_ms or 0.0,
            len(rows),
            (perf_counter() - started) * 1000,
        )
    return rows


def iter_rows(sql: str, params: Sequence[Any] | None = None, *, batch_size: int = 500) -> Iterator[dict[str, Any]]:
    started = perf_counter() if _stage_timing_enabled() else None
    with borrow_read_connection() as conn:
        with conn.cursor(pymysql.cursors.SSDictCursor) as cur:
            execute_started = perf_counter() if started is not None else None
            cur.execute(sql, params or ())
            execute_ms = (perf_counter() - execute_started) * 1000 if execute_started is not None else None
            rows_count = 0
            while True:
                fetch_started = perf_counter() if started is not None else None
                rows = cur.fetchmany(batch_size)
                fetch_ms = (perf_counter() - fetch_started) * 1000 if fetch_started is not None else None
                if not rows:
                    break
                rows_count += len(rows)
                yield from rows
    if started is not None:
        logger.info(
            "market_latency_db op=iter_rows table=%s execute_ms=%.3f fetch_ms=%.3f rows=%d total_ms=%.3f",
            _sql_table(sql),
            execute_ms or 0.0,
            (perf_counter() - started) * 1000 - (execute_ms or 0.0),
            rows_count,
            (perf_counter() - started) * 1000,
        )


def fetch_one(sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    with connect() as conn:
        with conn.cursor() as cur:
            affected = cur.execute(sql, params or ())
        conn.commit()
    return affected
