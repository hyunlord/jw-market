"""G-21: the mysql ledger must serialize concurrent access to its single shared
connection.

The trigger service runs sync endpoints in an anyio threadpool, so many requests
call ``Ledger._execute`` concurrently against ONE shared pymysql connection.
pymysql connections are NOT thread-safe — concurrent use corrupts the wire
protocol, observed live as::

    struct.error: unpack_from requires a buffer of at least 9 bytes ...
    AttributeError: 'NoneType' object has no attribute 'settimeout'

→ HTTP 500 under the site's status polling. (The per-call ``ping(reconnect=True)``
made it worse: reconnect nulls ``self._sock`` while another thread reads it.)

Fix: a ``threading.Lock`` serializes the whole ``_execute`` (run + reconnect +
retry) so the connection is only ever touched by one thread at a time.

The fake below can't reproduce pymysql's byte-level corruption, but it records the
peak number of threads simultaneously inside a query — which *is* the root cause.
peak > 1 == the bug; the lock must hold peak == 1.
"""
from __future__ import annotations

import threading
import time

from pipeline.scripts.ingest_hook.ledger import Ledger

IDENTITY = ("2026-03", "ubist", "z" * 64)


class _ProbeCursor:
    def __init__(self, conn: "_ConcurrencyProbeConn") -> None:
        self._conn = conn

    def execute(self, sql: str, params: tuple = ()):
        self._conn._enter()
        try:
            time.sleep(0.02)  # widen the window so overlapping threads are observed
        finally:
            self._conn._leave()

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self) -> None:
        pass


class _ConcurrencyProbeConn:
    """Fake mysql conn recording peak threads simultaneously inside a query."""

    def __init__(self) -> None:
        self._depth = 0
        self.peak = 0
        self._bk = threading.Lock()  # protects bookkeeping ONLY, not the unit under test
        self.ping_calls = 0

    def cursor(self) -> _ProbeCursor:
        return _ProbeCursor(self)

    def commit(self) -> None:
        pass

    def ping(self, reconnect: bool = False) -> None:
        with self._bk:
            self.ping_calls += 1

    def _enter(self) -> None:
        with self._bk:
            self._depth += 1
            if self._depth > self.peak:
                self.peak = self._depth

    def _leave(self) -> None:
        with self._bk:
            self._depth -= 1


def test_execute_serializes_concurrent_mysql_access():
    conn = _ConcurrencyProbeConn()
    ledger = Ledger(conn, dialect="mysql")

    n = 12
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker() -> None:
        barrier.wait()  # release all workers together -> maximal overlap
        try:
            ledger.status(*IDENTITY)  # -> _fetch_row -> _execute
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"unexpected errors under concurrency: {errors!r}"
    assert conn.peak == 1, (
        f"single connection touched by {conn.peak} threads at once — not serialized "
        "(this is the struct.error/settimeout corruption path)"
    )
