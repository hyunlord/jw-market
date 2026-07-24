"""G-1: mysql ledger survives a Galera ``wait_timeout`` idle-closed connection.

The production ledger holds one long-lived ``pymysql`` connection (config.py,
``autocommit=False``). Galera silently closes it after ``wait_timeout`` of idle,
so the next webhook/status request used to surface as ``OperationalError(2006,
'MySQL server has gone away')`` / ``InterfaceError(0, '')`` → HTTP 500. The fix
adds, inside the single choke point ``Ledger._execute`` (mysql dialect only):

  * W-1  ping(reconnect=True) before every statement — revive an idle-closed socket
  * W-2  on a stale error mid-statement (TOCTOU), reconnect and retry exactly once;
         a second consecutive death raises ``LedgerConnectionError`` (clear 5xx,
         never a silent success).

Faking policy: the connection *death* cannot be produced for real in a unit test,
so it is faked — but the SQL runs for real against an in-memory sqlite backend
(mysql ``%s`` placeholders translated back to ``?``), and the errors raised are
the genuine ``pymysql`` classes, so the ledger's stale-detection is exercised
exactly as in production. The sqlite dialect must be wholly unaffected (it has no
``.ping()``); that is pinned too.
"""
from __future__ import annotations

import sqlite3

import pymysql
import pytest

import pipeline.scripts.ingest_hook.ledger as ledger_mod
from pipeline.scripts.ingest_hook.ledger import Ledger

IDENTITY = ("2026-03", "ubist", "a" * 64)


class _FakeCursor:
    def __init__(self, conn: "FakeGaleraConnection") -> None:
        self._conn = conn
        self._sqlite_cursor: sqlite3.Cursor | None = None

    def execute(self, sql: str, params: tuple = ()):  # noqa: D401
        self._conn._before_execute()  # may raise a genuine pymysql stale error
        self._conn.executed_sql.append(sql)
        translated = sql.replace("%s", "?").removesuffix(" FOR UPDATE")
        translated = translated.split(" ON DUPLICATE KEY UPDATE", 1)[0]
        self._sqlite_cursor = self._conn._backend.execute(translated, params)
        return self._sqlite_cursor

    def fetchone(self):
        return self._sqlite_cursor.fetchone()

    def fetchall(self):
        return self._sqlite_cursor.fetchall()

    def close(self) -> None:
        if self._sqlite_cursor is not None:
            self._sqlite_cursor.close()


class FakeGaleraConnection:
    """pymysql-shaped connection over an in-memory sqlite backend that can die.

    * ``start_dead``      — socket begins closed (idle wait_timeout): the first
                            statement raises unless ``ping(reconnect=True)`` revives it.
    * ``execute_deaths``  — that many upcoming ``execute()`` calls raise a stale
                            error and close the socket (models TOCTOU / permanent death).
    * ``reconnectable``   — whether ``ping(reconnect=True)`` can revive the socket;
                            False models a genuinely unreachable server.
    * ``stale_error``     — the pymysql error instance a dead statement raises.
    """

    def __init__(
        self,
        *,
        start_dead: bool = False,
        reconnectable: bool = True,
        execute_deaths: int = 0,
        stale_error: BaseException | None = None,
    ) -> None:
        self._backend = sqlite3.connect(":memory:")
        self._backend.executescript(ledger_mod._DDL_SQLITE)
        self._backend.executescript(ledger_mod._DDL_TRANSITION_SQLITE)
        self._live = not start_dead
        self._reconnectable = reconnectable
        self._execute_deaths = execute_deaths
        self._stale_error = stale_error or pymysql.err.OperationalError(
            2006, "MySQL server has gone away"
        )
        self.ping_calls = 0
        self.execute_attempts = 0
        self.executed_sql: list[str] = []

    # -- pymysql surface the ledger relies on ------------------------------
    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def ping(self, reconnect: bool = False) -> None:
        self.ping_calls += 1
        if self._reconnectable:
            self._live = True
            return
        raise pymysql.err.OperationalError(
            2006, "MySQL server has gone away (ping/reconnect failed)"
        )

    def commit(self) -> None:
        if not self._live:
            raise pymysql.err.OperationalError(2006, "MySQL server has gone away (commit)")
        self._backend.commit()

    def rollback(self) -> None:
        self._backend.rollback()

    # -- death injection ----------------------------------------------------
    def _before_execute(self) -> None:
        self.execute_attempts += 1
        if self._execute_deaths > 0:
            self._execute_deaths -= 1
            self._live = False
            raise self._fresh_stale_error()
        if not self._live:
            raise self._fresh_stale_error()

    def _fresh_stale_error(self) -> BaseException:
        err = self._stale_error
        return type(err)(*err.args)  # fresh instance, mirrors driver behaviour


def _mysql_ledger(conn: FakeGaleraConnection) -> Ledger:
    return Ledger(conn, dialect="mysql")


# -- W-1: revive an idle wait_timeout-closed connection -----------------------
def test_receive_revives_idle_closed_connection_end_to_end():
    """A webhook arriving after wait_timeout must succeed, not 500."""
    conn = FakeGaleraConnection(start_dead=True)  # idle-closed socket
    ledger = _mysql_ledger(conn)

    decision = ledger.receive(*IDENTITY, manifest_path="/x/manifest.json", uploaded_by="pl@jw")

    assert decision.action == "queued"  # would raise OperationalError(2006) pre-fix
    assert ledger.status(*IDENTITY).status == "queued"
    assert conn.ping_calls >= 1  # W-1 ping revived the socket before use


def test_status_revives_idle_closed_connection():
    """GET /ingest/status path (InterfaceError symptom) also survives idle death."""
    conn = FakeGaleraConnection(
        start_dead=True, stale_error=pymysql.err.InterfaceError(0, "")
    )
    ledger = _mysql_ledger(conn)

    assert ledger.status(*IDENTITY) is None  # unknown identity, but NO 500
    assert conn.ping_calls >= 1


def test_mysql_transition_locks_identity_before_status_change():
    conn = FakeGaleraConnection()
    ledger = _mysql_ledger(conn)
    ledger.receive(*IDENTITY, manifest_path="/x/manifest.json")

    ledger.mark_running(
        *IDENTITY,
        job_name="jw-ingest-ubist-lock-test",
        run_id="run-lock-test",
    )

    assert any(
        sql.endswith(" FOR UPDATE")
        for sql in conn.executed_sql
        if sql.startswith("SELECT status, job_name FROM ingest_ledger")
    )


# -- W-2: reconnect + retry-once on a mid-statement death (TOCTOU) -------------
def test_reconnects_and_retries_once_on_toctou_death():
    """Socket alive at ping, dies on the execute, then one reconnect+retry succeeds."""
    conn = FakeGaleraConnection(execute_deaths=1)  # dies exactly once, mid-statement
    ledger = _mysql_ledger(conn)

    assert ledger.status(*IDENTITY) is None  # succeeds after a single retry
    assert conn.execute_attempts == 2  # first death + one retry
    assert conn.ping_calls == 1  # W-2 reconnect only (no proactive per-call ping)


def test_interface_error_mid_statement_is_treated_as_stale_and_retried():
    conn = FakeGaleraConnection(
        execute_deaths=1, stale_error=pymysql.err.InterfaceError(0, "")
    )
    ledger = _mysql_ledger(conn)

    assert ledger.status(*IDENTITY) is None
    assert conn.execute_attempts == 2


# -- W-2 terminal: two consecutive deaths -> clear error, never silent ---------
def test_two_consecutive_deaths_raise_ledger_connection_error():
    conn = FakeGaleraConnection(execute_deaths=99)  # every statement dies
    ledger = _mysql_ledger(conn)

    with pytest.raises(ledger_mod.LedgerConnectionError) as exc_info:
        ledger.status(*IDENTITY)

    assert str(exc_info.value)  # a clear, non-empty error body (no silent failure)
    assert conn.execute_attempts == 2  # tried once, reconnected, retried once, then gave up


def test_unreachable_server_ping_failure_raises_ledger_connection_error():
    """When the server is genuinely down, ping(reconnect) fails -> clear 5xx."""
    conn = FakeGaleraConnection(start_dead=True, reconnectable=False)
    ledger = _mysql_ledger(conn)

    with pytest.raises(ledger_mod.LedgerConnectionError):
        ledger.status(*IDENTITY)


# -- scope guard: real SQL/operational errors are NOT swallowed or retried -----
def test_non_stale_operational_error_propagates_without_retry():
    """A real SQL error (e.g. 1146 table missing) must surface unchanged, once."""
    conn = FakeGaleraConnection(
        execute_deaths=1,
        stale_error=pymysql.err.OperationalError(1146, "Table 'ingest_ledger' doesn't exist"),
    )
    ledger = _mysql_ledger(conn)

    with pytest.raises(pymysql.err.OperationalError) as exc_info:
        ledger.status(*IDENTITY)

    assert exc_info.value.args[0] == 1146  # original error, not LedgerConnectionError
    assert conn.execute_attempts == 1  # no retry for a non-connection error


# -- dialect guard: sqlite path never touches ping ----------------------------
def test_sqlite_dialect_is_unaffected_by_reconnect_path():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    ledger = Ledger(conn, dialect="sqlite")
    ledger.ensure_table()

    assert ledger.receive(*IDENTITY, manifest_path="/x/manifest.json").action == "queued"
    assert ledger.status(*IDENTITY).status == "queued"
    assert not hasattr(conn, "ping")  # sqlite has no ping; the dialect gate must skip W-1
