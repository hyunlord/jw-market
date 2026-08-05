"""ingest_ledger — idempotency lock + status source of truth.

Identity is ``(epoch, category, manifest_sha)``. Repeated webhooks for the same
identity are no-ops while the row is queued/running/complete; only ``failed``
rows may be re-queued. One ``running`` row per category serialises loads inside
a category; different categories run in parallel.

Two dialects on purpose:
  * ``mysql``  — production ledger in the mart DB (MARIADB_* env family).
    The table is only created on explicit activation (PL gate).
  * ``sqlite`` — isolation rehearsals and tests; identical semantics with zero
    production contact.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# pymysql lives only in the production (mysql) image; sqlite rehearsals and unit
# tests must import this module without it. Guard the import so the sqlite path
# stays dependency-free, and only reference these types on the mysql code path.
try:  # pragma: no cover - trivial import guard
    from pymysql.err import (
        InterfaceError as _MySQLInterfaceError,
        OperationalError as _MySQLOperationalError,
    )

    _STALE_CONN_ERRORS = (_MySQLOperationalError, _MySQLInterfaceError)
except Exception:  # pragma: no cover - sqlite-only environments have no pymysql
    _MySQLInterfaceError = None
    _MySQLOperationalError = None
    _STALE_CONN_ERRORS = ()

# MySQL error codes meaning "the connection is dead; the statement did not run":
#   2006 = server has gone away (Galera closed an idle wait_timeout connection)
#   2013 = lost connection during query
_MYSQL_GONE_AWAY_CODES = (2006, 2013)


class LedgerConnectionError(RuntimeError):
    """The mysql ledger connection could not be revived (ping + reconnect + one
    retry all failed).

    Raised instead of leaking a raw driver error or — worse — failing silently.
    The trigger service maps it to a clear HTTP 5xx body; batch callers let it
    propagate to their logs. Only genuine connection loss maps here: real SQL
    errors (syntax, integrity, lock timeout) propagate unchanged.
    """


def _is_stale_connection_error(exc: BaseException) -> bool:
    """True only for a dead/closed connection — never for a real SQL error."""
    if _MySQLInterfaceError is not None and isinstance(exc, _MySQLInterfaceError):
        return True
    if _MySQLOperationalError is not None and isinstance(exc, _MySQLOperationalError):
        code = exc.args[0] if exc.args else None
        return code in _MYSQL_GONE_AWAY_CODES
    return False


STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
STATUS_GATE_FAILED = "gate_failed"
STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_PUBLISH_RUNNING = "publish_running"
_HELD_STATUSES = (
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_AWAITING_APPROVAL,
    STATUS_PUBLISH_RUNNING,
    STATUS_COMPLETE,
)
_CATEGORY_BLOCKING_STATUSES = (
    STATUS_RUNNING,
    STATUS_AWAITING_APPROVAL,
    STATUS_PUBLISH_RUNNING,
)

_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS ingest_ledger (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  epoch         TEXT NOT NULL,
  category      TEXT NOT NULL,
  manifest_sha  TEXT NOT NULL,
  manifest_path TEXT NOT NULL,
  uploaded_by   TEXT,
  status        TEXT NOT NULL,
  reason        TEXT,
  job_name      TEXT,
  run_id        TEXT,
  row_counts    TEXT,
  received_at   TEXT NOT NULL,
  started_at    TEXT,
  finished_at   TEXT,
  UNIQUE (epoch, category, manifest_sha)
)
"""

_DDL_MYSQL = """
CREATE TABLE IF NOT EXISTS ingest_ledger (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  epoch         VARCHAR(32)  NOT NULL,
  category      VARCHAR(32)  NOT NULL,
  manifest_sha  CHAR(64)     NOT NULL,
  manifest_path VARCHAR(512) NOT NULL,
  uploaded_by   VARCHAR(128) NULL,
  status        VARCHAR(32)  NOT NULL,
  reason        TEXT         NULL,
  job_name      VARCHAR(128) NULL,
  run_id        VARCHAR(64)  NULL,
  row_counts    TEXT         NULL,
  received_at   DATETIME     NOT NULL,
  started_at    DATETIME     NULL,
  finished_at   DATETIME     NULL,
  UNIQUE KEY uq_ledger_identity (epoch, category, manifest_sha),
  KEY idx_ledger_category_status (category, status),
  KEY idx_ledger_run_id_id (run_id, id)
)
"""


# Per-stage observation table. Separate from ingest_ledger (not extra columns)
# because one submission runs N stages and may be retried under new run_ids —
# a 1:N child keyed by the ledger identity + run_id + seq. Purely observational:
# a write failure here must never fail the load (see Ledger.record_stage).
STAGE_RUNNING = "running"
STAGE_COMPLETE = "complete"
STAGE_FAILED = "failed"
STAGE_SKIPPED = "skipped"

_DDL_STAGE_SQLITE = """
CREATE TABLE IF NOT EXISTS ingest_stage_event (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  epoch         TEXT NOT NULL,
  category      TEXT NOT NULL,
  manifest_sha  TEXT NOT NULL,
  run_id        TEXT NOT NULL,
  seq           INTEGER NOT NULL,
  stage         TEXT NOT NULL,
  status        TEXT NOT NULL,
  reason        TEXT,
  started_at    TEXT,
  finished_at   TEXT,
  duration_ms   INTEGER,
  UNIQUE (epoch, category, manifest_sha, run_id, seq)
)
"""

_DDL_STAGE_MYSQL = """
CREATE TABLE IF NOT EXISTS ingest_stage_event (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  epoch         VARCHAR(32)  NOT NULL,
  category      VARCHAR(32)  NOT NULL,
  manifest_sha  CHAR(64)     NOT NULL,
  run_id        VARCHAR(64)  NOT NULL,
  seq           INT          NOT NULL,
  stage         VARCHAR(32)  NOT NULL,
  status        VARCHAR(32)  NOT NULL,
  reason        TEXT         NULL,
  started_at    DATETIME     NULL,
  finished_at   DATETIME     NULL,
  duration_ms   BIGINT       NULL,
  UNIQUE KEY uq_stage_identity (epoch, category, manifest_sha, run_id, seq),
  KEY idx_stage_lookup (epoch, category, manifest_sha)
)
"""

_DDL_SIGNAL_SQLITE = """
CREATE TABLE IF NOT EXISTS ingest_signal_event (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  epoch           TEXT NOT NULL,
  category        TEXT NOT NULL,
  manifest_sha    TEXT NOT NULL,
  run_id          TEXT NOT NULL,
  event           TEXT NOT NULL,
  mode            TEXT NOT NULL,
  rows_loaded     INTEGER NOT NULL,
  delivery_status TEXT NOT NULL,
  attempts        INTEGER NOT NULL,
  reason          TEXT,
  payload_json    TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  UNIQUE (epoch, category, manifest_sha, event)
)
"""

_DDL_SIGNAL_MYSQL = """
CREATE TABLE IF NOT EXISTS ingest_signal_event (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  epoch           VARCHAR(32) NOT NULL,
  category        VARCHAR(32) NOT NULL,
  manifest_sha    CHAR(64) NOT NULL,
  run_id          VARCHAR(64) NOT NULL,
  event           VARCHAR(16) NOT NULL,
  mode            VARCHAR(16) NOT NULL,
  rows_loaded     BIGINT NOT NULL,
  delivery_status VARCHAR(16) NOT NULL,
  attempts        INT NOT NULL,
  reason          TEXT NULL,
  payload_json    LONGTEXT NOT NULL,
  created_at      DATETIME NOT NULL,
  UNIQUE KEY uq_signal_identity (epoch, category, manifest_sha, event),
  KEY idx_signal_lookup (epoch, category, manifest_sha)
)
"""

_DDL_TRANSITION_SQLITE = """
CREATE TABLE IF NOT EXISTS ingest_status_transition (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id        TEXT NOT NULL UNIQUE,
  epoch           TEXT NOT NULL,
  category        TEXT NOT NULL,
  manifest_sha    TEXT NOT NULL,
  previous_status TEXT,
  status          TEXT NOT NULL,
  actor           TEXT NOT NULL,
  source          TEXT NOT NULL,
  reason          TEXT,
  job_name        TEXT,
  evidence_json   TEXT NOT NULL,
  created_at      TEXT NOT NULL
)
"""

_DDL_TRANSITION_MYSQL = """
CREATE TABLE IF NOT EXISTS ingest_status_transition (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  event_id        CHAR(36) NOT NULL,
  epoch           VARCHAR(32) NOT NULL,
  category        VARCHAR(32) NOT NULL,
  manifest_sha    CHAR(64) NOT NULL,
  previous_status VARCHAR(32) NULL,
  status          VARCHAR(32) NOT NULL,
  actor           VARCHAR(64) NOT NULL,
  source          VARCHAR(64) NOT NULL,
  reason          TEXT NULL,
  job_name        VARCHAR(128) NULL,
  evidence_json   LONGTEXT NOT NULL,
  created_at      DATETIME NOT NULL,
  UNIQUE KEY uq_status_transition_event (event_id),
  KEY idx_status_transition_identity (epoch, category, manifest_sha, id)
)
"""

_DDL_CANDIDATE_SQLITE = """
CREATE TABLE IF NOT EXISTS ingest_publish_candidate (
  epoch            TEXT NOT NULL,
  category         TEXT NOT NULL,
  manifest_sha     TEXT NOT NULL,
  build_run_id     TEXT NOT NULL,
  publish_job_name TEXT,
  payload_json     TEXT NOT NULL,
  prepared_at      TEXT NOT NULL,
  expires_at       TEXT NOT NULL,
  approved_at      TEXT,
  approved_by      TEXT,
  PRIMARY KEY (epoch, category, manifest_sha)
)
"""

_DDL_CANDIDATE_MYSQL = """
CREATE TABLE IF NOT EXISTS ingest_publish_candidate (
  epoch            VARCHAR(32) NOT NULL,
  category         VARCHAR(32) NOT NULL,
  manifest_sha     CHAR(64) NOT NULL,
  build_run_id     VARCHAR(64) NOT NULL,
  publish_job_name VARCHAR(128) NULL,
  payload_json     LONGTEXT NOT NULL,
  prepared_at      DATETIME NOT NULL,
  expires_at       DATETIME NOT NULL,
  approved_at      DATETIME NULL,
  approved_by      VARCHAR(128) NULL,
  PRIMARY KEY (epoch, category, manifest_sha)
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _utc_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class LedgerEntry:
    epoch: str
    category: str
    manifest_sha: str
    manifest_path: str
    uploaded_by: str | None
    status: str
    reason: str | None
    job_name: str | None
    run_id: str | None
    row_counts: dict[str, int] | None
    received_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True)
class ReceiveDecision:
    action: str  # queued | noop
    status: str  # current row status
    reason: str


@dataclass(frozen=True)
class StageEvent:
    run_id: str
    seq: int
    stage: str
    status: str  # running | complete | failed | skipped
    reason: str | None
    started_at: str | None
    finished_at: str | None
    duration_ms: int | None


@dataclass(frozen=True)
class SignalEvent:
    run_id: str
    event: str
    mode: str
    rows_loaded: int
    delivery_status: str
    attempts: int
    reason: str | None
    payload: dict
    created_at: str


@dataclass(frozen=True)
class StatusTransition:
    event_id: str
    previous_status: str | None
    status: str
    actor: str
    source: str
    reason: str | None
    job_name: str | None
    evidence: dict
    created_at: str


@dataclass(frozen=True)
class PreparedCandidate:
    epoch: str
    category: str
    manifest_sha: str
    build_run_id: str
    publish_job_name: str | None
    payload: dict
    prepared_at: str
    expires_at: str
    approved_at: str | None
    approved_by: str | None


class Ledger:
    """Dialect-neutral ledger operations over an injected DB-API connection."""

    def __init__(self, conn, dialect: str = "sqlite"):
        if dialect not in ("sqlite", "mysql"):
            raise ValueError(f"unknown dialect {dialect!r}")
        self._conn = conn
        self._dialect = dialect
        self._mark = "?" if dialect == "sqlite" else "%s"
        # The trigger service shares this one connection across a request
        # threadpool; DB-API connections are not thread-safe, so every _execute
        # is serialized (a single shared connection touched by >1 thread corrupts
        # the pymysql wire protocol -> struct.error / 'NoneType'.settimeout -> 500).
        self._lock = threading.RLock()

    # -- schema ------------------------------------------------------------
    def ensure_table(self) -> None:
        cursor = self._conn.cursor()
        cursor.execute(_DDL_SQLITE if self._dialect == "sqlite" else _DDL_MYSQL)
        cursor.execute(_DDL_STAGE_SQLITE if self._dialect == "sqlite" else _DDL_STAGE_MYSQL)
        cursor.execute(_DDL_SIGNAL_SQLITE if self._dialect == "sqlite" else _DDL_SIGNAL_MYSQL)
        cursor.execute(
            _DDL_TRANSITION_SQLITE
            if self._dialect == "sqlite"
            else _DDL_TRANSITION_MYSQL
        )
        cursor.execute(
            _DDL_CANDIDATE_SQLITE
            if self._dialect == "sqlite"
            else _DDL_CANDIDATE_MYSQL
        )
        self._conn.commit()

    # -- helpers -----------------------------------------------------------
    def _execute(self, sql: str, params: tuple = ()):
        statement = sql.replace("?", self._mark) if self._dialect == "mysql" else sql
        # Serialize all access to the single injected connection (thread-safety).
        with self._lock:
            if self._dialect != "mysql":
                # sqlite (tests/rehearsals): local connection, never idle-closed,
                # and no .ping(); run directly (still serialized for uniformity).
                return self._run(statement, params)
            return self._execute_resilient(statement, params)

    def _run(self, statement: str, params: tuple):
        cursor = self._conn.cursor()
        cursor.execute(statement, params)
        self._conn.commit()
        return cursor

    def _execute_resilient(self, statement: str, params: tuple):
        """mysql only: survive a Galera ``wait_timeout`` idle-closed connection.

        Runs under ``self._lock`` (see ``_execute``), so the shared connection is
        touched by one thread at a time — this alone removes the concurrent-use
        corruption (``struct.error`` / ``'NoneType'.settimeout`` → 500).

        For wait_timeout idle death: the first statement on a dropped connection
        raises ``OperationalError(2006)`` / ``InterfaceError``; on that (and only
        that) error, reconnect once and retry exactly once. A second consecutive
        death is a real outage → ``LedgerConnectionError`` (clear 5xx, never a
        silent success).

        No proactive per-call ``ping(reconnect=True)``: on a shared connection it
        added a wasted round-trip AND a reconnect-mid-use race (it nulls the socket
        while another thread reads it) that aggravated the concurrency corruption.
        The catch-reconnect-retry below is the single defense. A per-request
        connection is deliberately NOT used (injected long-lived-connection
        contract); ``wait_timeout`` is never changed here. Real SQL errors
        (integrity, lock timeout, syntax) are never retried or masked.
        """
        try:
            return self._run(statement, params)
        except _STALE_CONN_ERRORS as exc:
            if not _is_stale_connection_error(exc):
                raise  # a real SQL/operational error — surface it unchanged

        # The connection was idle-closed by wait_timeout. Reconnect + retry once.
        try:
            self._conn.ping(reconnect=True)
        except Exception as exc:
            raise LedgerConnectionError(
                f"ingest ledger DB unreachable on reconnect: {exc}"
            ) from exc
        try:
            return self._run(statement, params)
        except _STALE_CONN_ERRORS as exc:
            if not _is_stale_connection_error(exc):
                raise  # a real SQL error on retry — surface it unchanged
            raise LedgerConnectionError(
                "ingest ledger DB connection unavailable after reconnect and one "
                f"retry: {exc}"
            ) from exc

    def _transaction(self, operation):
        """Run one ledger mutation atomically, reconnecting only before a retry.

        ``operation`` receives a cursor and must not commit. A dead MySQL
        connection rolls its open transaction back server-side; the same
        operation is retried once after reconnect. Transition inserts use a
        stable event id and duplicate-safe SQL so an ambiguous commit response
        cannot create duplicate history.
        """
        with self._lock:
            for attempt in (1, 2):
                cursor = self._conn.cursor()
                try:
                    result = operation(cursor)
                    self._conn.commit()
                    return result
                except _STALE_CONN_ERRORS as exc:
                    try:
                        self._conn.rollback()
                    except Exception:
                        pass
                    if (
                        self._dialect != "mysql"
                        or not _is_stale_connection_error(exc)
                        or attempt == 2
                    ):
                        if self._dialect == "mysql" and _is_stale_connection_error(exc):
                            raise LedgerConnectionError(
                                "ingest ledger DB connection unavailable during "
                                f"transaction: {exc}"
                            ) from exc
                        raise
                    try:
                        self._conn.ping(reconnect=True)
                    except Exception as reconnect_exc:
                        raise LedgerConnectionError(
                            "ingest ledger DB unreachable on transaction reconnect: "
                            f"{reconnect_exc}"
                        ) from reconnect_exc
                except Exception:
                    try:
                        self._conn.rollback()
                    except Exception:
                        pass
                    raise
        raise AssertionError("transaction retry loop exhausted")

    def _transition(
        self,
        epoch: str,
        category: str,
        manifest_sha: str,
        *,
        status: str,
        assignments: str,
        values: tuple,
        actor: str,
        source: str,
        reason: str | None = None,
        evidence: dict | None = None,
        expected_status: str | None = None,
        expected_job_name: str | None = None,
        expected_run_id: str | None = None,
    ) -> bool:
        event_id = str(uuid.uuid4())
        created_at = _now()
        evidence_json = json.dumps(
            evidence or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

        def operation(cursor):
            return self._transition_with_cursor(
                cursor,
                epoch,
                category,
                manifest_sha,
                status=status,
                assignments=assignments,
                values=values,
                actor=actor,
                source=source,
                reason=reason,
                evidence_json=evidence_json,
                event_id=event_id,
                created_at=created_at,
                expected_status=expected_status,
                expected_job_name=expected_job_name,
                expected_run_id=expected_run_id,
            )

        return bool(self._transaction(operation))

    def _transition_with_cursor(
        self,
        cursor,
        epoch: str,
        category: str,
        manifest_sha: str,
        *,
        status: str,
        assignments: str,
        values: tuple,
        actor: str,
        source: str,
        reason: str | None,
        evidence_json: str,
        event_id: str,
        created_at: str,
        expected_status: str | None = None,
        expected_job_name: str | None = None,
        expected_run_id: str | None = None,
        before_update=None,
    ) -> bool:
        """Apply one transition on an existing transaction cursor.

        Candidate mutations use ``before_update`` so the candidate row and ledger
        status share one commit and one lock order (ledger, then candidate).
        """
        mark = self._mark
        select_sql = (
            "SELECT status, job_name, run_id FROM ingest_ledger"
            f" WHERE epoch={mark} AND category={mark} AND manifest_sha={mark}"
        )
        if self._dialect == "mysql":
            select_sql += " FOR UPDATE"
        update_sql = (
            f"UPDATE ingest_ledger SET {assignments}"
            f" WHERE epoch={mark} AND category={mark} AND manifest_sha={mark}"
        )
        history_sql = (
            "INSERT INTO ingest_status_transition"
            " (event_id, epoch, category, manifest_sha, previous_status, status,"
            " actor, source, reason, job_name, evidence_json, created_at)"
            f" VALUES ({', '.join([mark] * 12)})"
        )
        if self._dialect == "sqlite":
            history_sql += " ON CONFLICT(event_id) DO NOTHING"
        else:
            history_sql += " ON DUPLICATE KEY UPDATE event_id=VALUES(event_id)"

        cursor.execute(select_sql, (epoch, category, manifest_sha))
        row = cursor.fetchone()
        if row is None:
            return False
        values_row = tuple(row.values()) if isinstance(row, dict) else tuple(row)
        previous_status, job_name, run_id = (
            str(values_row[0]),
            values_row[1],
            values_row[2],
        )
        if expected_status is not None and previous_status != expected_status:
            return False
        if expected_job_name is not None and job_name != expected_job_name:
            return False
        if expected_run_id is not None and run_id != expected_run_id:
            return False
        if before_update is not None and not before_update(cursor):
            return False
        cursor.execute(update_sql, values + (epoch, category, manifest_sha))
        cursor.execute(
            history_sql,
            (
                event_id,
                epoch,
                category,
                manifest_sha,
                previous_status,
                status,
                actor,
                source,
                reason[:4000] if reason else None,
                job_name,
                evidence_json,
                created_at,
            ),
        )
        return True

    def _insert_queued(
        self,
        epoch: str,
        category: str,
        manifest_sha: str,
        manifest_path: str,
        uploaded_by: str | None,
    ) -> None:
        event_id = str(uuid.uuid4())
        created_at = _now()
        mark = self._mark
        ledger_sql = (
            "INSERT INTO ingest_ledger"
            " (epoch, category, manifest_sha, manifest_path, uploaded_by, status, received_at)"
            f" VALUES ({', '.join([mark] * 7)})"
        )
        history_sql = (
            "INSERT INTO ingest_status_transition"
            " (event_id, epoch, category, manifest_sha, previous_status, status,"
            " actor, source, reason, job_name, evidence_json, created_at)"
            f" VALUES ({', '.join([mark] * 12)})"
        )

        def operation(cursor):
            cursor.execute(
                ledger_sql,
                (
                    epoch,
                    category,
                    manifest_sha,
                    manifest_path,
                    uploaded_by,
                    STATUS_QUEUED,
                    created_at,
                ),
            )
            cursor.execute(
                history_sql,
                (
                    event_id,
                    epoch,
                    category,
                    manifest_sha,
                    None,
                    STATUS_QUEUED,
                    "ingest_service",
                    "webhook_receive",
                    "new submission queued",
                    None,
                    "{}",
                    created_at,
                ),
            )

        self._transaction(operation)

    def _receive_mysql_atomic(
        self,
        epoch: str,
        category: str,
        manifest_sha: str,
        manifest_path: str,
        uploaded_by: str | None,
    ) -> ReceiveDecision:
        """Materialize, lock, and decide one MariaDB identity atomically."""
        event_id = str(uuid.uuid4())
        created_at = _now()
        mark = self._mark
        insert_sql = (
            "INSERT INTO ingest_ledger"
            " (epoch, category, manifest_sha, manifest_path, uploaded_by, status, received_at)"
            f" VALUES ({', '.join([mark] * 7)})"
            " ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)"
        )
        select_sql = (
            "SELECT status, job_name, run_id FROM ingest_ledger"
            f" WHERE epoch={mark} AND category={mark} AND manifest_sha={mark}"
            " FOR UPDATE"
        )
        update_sql = (
            "UPDATE ingest_ledger"
            f" SET status={mark}, reason={mark}, received_at={mark}, uploaded_by={mark}"
            f" WHERE epoch={mark} AND category={mark} AND manifest_sha={mark}"
            f" AND status={mark}"
        )
        history_sql = (
            "INSERT INTO ingest_status_transition"
            " (event_id, epoch, category, manifest_sha, previous_status, status,"
            " actor, source, reason, job_name, evidence_json, created_at)"
            f" VALUES ({', '.join([mark] * 12)})"
            " ON DUPLICATE KEY UPDATE event_id=VALUES(event_id)"
        )

        def append_history(
            cursor,
            *,
            previous_status: str | None,
            source: str,
            reason: str,
            job_name: str | None,
        ) -> None:
            cursor.execute(
                history_sql,
                (
                    event_id,
                    epoch,
                    category,
                    manifest_sha,
                    previous_status,
                    STATUS_QUEUED,
                    "ingest_service",
                    source,
                    reason,
                    job_name,
                    "{}",
                    created_at,
                ),
            )

        def operation(cursor):
            cursor.execute(
                insert_sql,
                (
                    epoch,
                    category,
                    manifest_sha,
                    manifest_path,
                    uploaded_by,
                    STATUS_QUEUED,
                    created_at,
                ),
            )
            inserted = cursor.rowcount == 1
            cursor.execute(select_sql, (epoch, category, manifest_sha))
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("duplicate-safe ledger insert did not materialize a row")
            values = tuple(row.values()) if isinstance(row, dict) else tuple(row)
            status, job_name = str(values[0]), values[1]

            if inserted:
                append_history(
                    cursor,
                    previous_status=None,
                    source="webhook_receive",
                    reason="new submission queued",
                    job_name=None,
                )
                return ReceiveDecision(
                    "queued", STATUS_QUEUED, "new submission queued"
                )
            if status in _HELD_STATUSES:
                return ReceiveDecision(
                    "noop", status, f"identity already {status}; webhook ignored"
                )

            reason = "re-queued after failure"
            cursor.execute(
                update_sql,
                (
                    STATUS_QUEUED,
                    reason,
                    created_at,
                    uploaded_by,
                    epoch,
                    category,
                    manifest_sha,
                    status,
                ),
            )
            if cursor.rowcount != 1:
                return ReceiveDecision(
                    "noop",
                    status,
                    "identity changed concurrently; retry not applied",
                )
            append_history(
                cursor,
                previous_status=status,
                source="webhook_retry",
                reason=reason,
                job_name=job_name,
            )
            return ReceiveDecision(
                "queued", STATUS_QUEUED, "previous attempt failed; re-queued"
            )

        return self._transaction(operation)

    def _fetch_row(self, epoch: str, category: str, manifest_sha: str):
        cursor = self._execute(
            "SELECT epoch, category, manifest_sha, manifest_path, uploaded_by, status, reason, job_name,"
            " run_id, row_counts, received_at, started_at, finished_at"
            " FROM ingest_ledger WHERE epoch=? AND category=? AND manifest_sha=?",
            (epoch, category, manifest_sha),
        )
        return cursor.fetchone()

    @staticmethod
    def _entry(row) -> LedgerEntry:
        values = tuple(row.values()) if isinstance(row, dict) else tuple(row)
        row_counts = json.loads(values[9]) if values[9] else None
        return LedgerEntry(
            epoch=values[0], category=values[1], manifest_sha=values[2], manifest_path=values[3],
            uploaded_by=values[4], status=values[5], reason=values[6], job_name=values[7],
            run_id=values[8],
            row_counts=row_counts,
            received_at=str(values[10]),
            started_at=str(values[11]) if values[11] else None,
            finished_at=str(values[12]) if values[12] else None,
        )

    # -- webhook receipt (idempotent) ---------------------------------------
    def receive(
        self,
        epoch: str,
        category: str,
        manifest_sha: str,
        manifest_path: str,
        uploaded_by: str | None = None,
    ) -> ReceiveDecision:
        if self._dialect == "mysql":
            return self._receive_mysql_atomic(
                epoch,
                category,
                manifest_sha,
                manifest_path,
                uploaded_by,
            )

        # SQLite is an intentionally independent shadow/test ledger. Keep its
        # existing SELECT -> transition behavior unchanged.
        existing = self._fetch_row(epoch, category, manifest_sha)
        if existing is not None:
            status = self._entry(existing).status
            if status in _HELD_STATUSES:
                return ReceiveDecision("noop", status, f"identity already {status}; webhook ignored")
            # failed -> allow retry
            reason = "re-queued after failure"
            self._transition(
                epoch,
                category,
                manifest_sha,
                status=STATUS_QUEUED,
                assignments=f"status={self._mark}, reason={self._mark}, received_at={self._mark}, uploaded_by={self._mark}",
                values=(STATUS_QUEUED, reason, _now(), uploaded_by),
                actor="ingest_service",
                source="webhook_retry",
                reason=reason,
            )
            return ReceiveDecision("queued", STATUS_QUEUED, "previous attempt failed; re-queued")
        self._insert_queued(epoch, category, manifest_sha, manifest_path, uploaded_by)
        return ReceiveDecision("queued", STATUS_QUEUED, "new submission queued")

    # -- category serialisation ---------------------------------------------
    def running_in_category(self, category: str) -> int:
        cursor = self._execute(
            "SELECT COUNT(*) FROM ingest_ledger WHERE category=? AND status=?",
            (category, STATUS_RUNNING),
        )
        row = cursor.fetchone()
        return int(tuple(row.values())[0] if isinstance(row, dict) else row[0])

    def next_queued(self, category: str) -> LedgerEntry | None:
        cursor = self._execute(
            "SELECT epoch, category, manifest_sha, manifest_path, uploaded_by, status, reason, job_name,"
            " run_id, row_counts, received_at, started_at, finished_at"
            " FROM ingest_ledger WHERE category=? AND status=? ORDER BY received_at, id LIMIT 1",
            (category, STATUS_QUEUED),
        )
        row = cursor.fetchone()
        return self._entry(row) if row is not None else None

    def queued_categories(self) -> list[str]:
        cursor = self._execute(
            "SELECT DISTINCT category FROM ingest_ledger WHERE status=?", (STATUS_QUEUED,)
        )
        return [
            (tuple(row.values())[0] if isinstance(row, dict) else row[0])
            for row in cursor.fetchall()
        ]

    def blocking_entries(self, category: str | None = None) -> list[LedgerEntry]:
        params: tuple[str, ...] = _CATEGORY_BLOCKING_STATUSES
        category_clause = ""
        if category is not None:
            category_clause = " AND category=?"
            params += (category,)
        marks = ",".join("?" for _status in _CATEGORY_BLOCKING_STATUSES)
        cursor = self._execute(
            "SELECT epoch, category, manifest_sha, manifest_path, uploaded_by, status, reason, job_name,"
            " run_id, row_counts, received_at, started_at, finished_at"
            f" FROM ingest_ledger WHERE status IN ({marks})"
            f"{category_clause}"
            " ORDER BY category, started_at, id",
            params,
        )
        return [self._entry(row) for row in cursor.fetchall()]

    def running_entries(self, category: str | None = None) -> list[LedgerEntry]:
        params: tuple[str, ...] = (STATUS_RUNNING,)
        category_clause = ""
        if category is not None:
            category_clause = " AND category=?"
            params += (category,)
        cursor = self._execute(
            "SELECT epoch, category, manifest_sha, manifest_path, uploaded_by, status, reason, job_name,"
            " run_id, row_counts, received_at, started_at, finished_at"
            f" FROM ingest_ledger WHERE status=?{category_clause}"
            " ORDER BY category, started_at, id",
            params,
        )
        return [self._entry(row) for row in cursor.fetchall()]

    def publish_running_entries(
        self, category: str | None = None
    ) -> list[LedgerEntry]:
        params: tuple[str, ...] = (STATUS_PUBLISH_RUNNING,)
        category_clause = ""
        if category is not None:
            category_clause = " AND category=?"
            params += (category,)
        cursor = self._execute(
            "SELECT epoch, category, manifest_sha, manifest_path, uploaded_by, status, reason, job_name,"
            " run_id, row_counts, received_at, started_at, finished_at"
            f" FROM ingest_ledger WHERE status=?{category_clause}"
            " ORDER BY category, started_at, id",
            params,
        )
        return [self._entry(row) for row in cursor.fetchall()]

    def active_entries(self, category: str | None = None) -> list[LedgerEntry]:
        active_statuses = (
            STATUS_RUNNING,
            STATUS_AWAITING_APPROVAL,
            STATUS_PUBLISH_RUNNING,
            STATUS_QUEUED,
        )
        params: tuple[str, ...] = active_statuses
        category_clause = ""
        if category is not None:
            category_clause = " AND category=?"
            params += (category,)
        marks = ",".join("?" for _status in active_statuses)
        statement = (
            "SELECT epoch, category, manifest_sha, manifest_path, uploaded_by, status, reason, job_name,"
            " run_id, row_counts, received_at, started_at, finished_at"
            f" FROM ingest_ledger WHERE status IN ({marks})"
            f"{category_clause}"
            " ORDER BY category,"
            " CASE status WHEN 'running' THEN 0 WHEN 'awaiting_approval' THEN 1"
            " WHEN 'publish_running' THEN 2 ELSE 3 END,"
            " received_at, id"
        )
        if self._dialect == "mysql":
            statement = statement.replace("?", self._mark)

        def operation(cursor):
            cursor.execute(statement, params)
            return cursor.fetchall()

        rows = self._transaction(operation)
        return [self._entry(row) for row in rows]

    def claim_queued(
        self,
        epoch: str,
        category: str,
        manifest_sha: str,
        *,
        job_name: str,
        run_id: str,
    ) -> bool:
        """Atomically reserve one queued identity when its category has no runner."""
        event_id = str(uuid.uuid4())
        created_at = _now()
        mark = self._mark
        active_sql = (
            "SELECT epoch, category, manifest_sha, status FROM ingest_ledger"
            f" WHERE category={mark} AND status IN ({mark},{mark},{mark},{mark})"
            " ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, received_at, id"
        )
        if self._dialect == "mysql":
            active_sql += " FOR UPDATE"
        update_sql = (
            "UPDATE ingest_ledger SET"
            f" status={mark}, job_name={mark}, run_id={mark}, started_at={mark}"
            f" WHERE epoch={mark} AND category={mark} AND manifest_sha={mark}"
            f" AND status={mark}"
        )
        history_sql = (
            "INSERT INTO ingest_status_transition"
            " (event_id, epoch, category, manifest_sha, previous_status, status,"
            " actor, source, reason, job_name, evidence_json, created_at)"
            f" VALUES ({', '.join([mark] * 12)})"
        )
        if self._dialect == "sqlite":
            history_sql += " ON CONFLICT(event_id) DO NOTHING"
        else:
            history_sql += " ON DUPLICATE KEY UPDATE event_id=VALUES(event_id)"

        def operation(cursor):
            cursor.execute(
                active_sql,
                (
                    category,
                    STATUS_RUNNING,
                    STATUS_AWAITING_APPROVAL,
                    STATUS_PUBLISH_RUNNING,
                    STATUS_QUEUED,
                ),
            )
            rows = cursor.fetchall()
            statuses = [
                str(row["status"] if isinstance(row, dict) else row[3])
                for row in rows
            ]
            if any(status in _CATEGORY_BLOCKING_STATUSES for status in statuses):
                return False
            candidate_present = any(
                (
                    str(row["epoch"] if isinstance(row, dict) else row[0]),
                    str(row["category"] if isinstance(row, dict) else row[1]),
                    str(
                        row["manifest_sha"]
                        if isinstance(row, dict)
                        else row[2]
                    ),
                )
                == (epoch, category, manifest_sha)
                for row in rows
            )
            if not candidate_present:
                return False
            cursor.execute(
                update_sql,
                (
                    STATUS_RUNNING,
                    job_name,
                    run_id,
                    created_at,
                    epoch,
                    category,
                    manifest_sha,
                    STATUS_QUEUED,
                ),
            )
            if cursor.rowcount != 1:
                return False
            cursor.execute(
                history_sql,
                (
                    event_id,
                    epoch,
                    category,
                    manifest_sha,
                    STATUS_QUEUED,
                    STATUS_RUNNING,
                    "ingest_service",
                    "job_reservation",
                    "category slot reserved before Kubernetes Job submission",
                    job_name,
                    json.dumps(
                        {"run_id": run_id, "job_name": job_name},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    created_at,
                ),
            )
            return True

        return bool(self._transaction(operation))

    # -- state transitions ---------------------------------------------------
    def mark_running(self, epoch: str, category: str, manifest_sha: str, *, job_name: str, run_id: str) -> None:
        self._transition(
            epoch,
            category,
            manifest_sha,
            status=STATUS_RUNNING,
            assignments=f"status={self._mark}, job_name={self._mark}, run_id={self._mark}, started_at={self._mark}",
            values=(STATUS_RUNNING, job_name, run_id, _now()),
            actor="ingest_service",
            source="job_submission",
            evidence={"run_id": run_id, "job_name": job_name},
        )

    def mark_complete(self, epoch: str, category: str, manifest_sha: str, *, row_counts: dict[str, int]) -> None:
        self._transition(
            epoch,
            category,
            manifest_sha,
            status=STATUS_COMPLETE,
            assignments=f"status={self._mark}, reason=NULL, row_counts={self._mark}, finished_at={self._mark}",
            values=(STATUS_COMPLETE, json.dumps(row_counts, ensure_ascii=False), _now()),
            actor="job_runner",
            source="runner_completion",
            evidence={"row_counts": row_counts},
        )

    def mark_failed(self, epoch: str, category: str, manifest_sha: str, *, reason: str) -> None:
        self._transition(
            epoch,
            category,
            manifest_sha,
            status=STATUS_FAILED,
            assignments=f"status={self._mark}, reason={self._mark}, finished_at={self._mark}",
            values=(STATUS_FAILED, reason[:4000], _now()),
            actor="job_runner",
            source="runner_failure",
            reason=reason,
        )

    def mark_gate_failed(self, epoch: str, category: str, manifest_sha: str, *, reason: str) -> None:
        self._transition(
            epoch,
            category,
            manifest_sha,
            status=STATUS_GATE_FAILED,
            assignments=f"status={self._mark}, reason={self._mark}, finished_at={self._mark}",
            values=(STATUS_GATE_FAILED, reason[:4000], _now()),
            actor="job_runner",
            source="gate_failure",
            reason=reason,
        )

    def mark_awaiting_approval(
        self,
        epoch: str,
        category: str,
        manifest_sha: str,
        *,
        run_id: str,
        candidate: dict,
        prepared_at: str,
        expires_at: str,
    ) -> None:
        payload_json = json.dumps(
            candidate,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        mark = self._mark
        upsert_sql = (
            "INSERT INTO ingest_publish_candidate"
            " (epoch, category, manifest_sha, build_run_id, payload_json, prepared_at, expires_at)"
            f" VALUES ({', '.join([mark] * 7)})"
        )
        if self._dialect == "sqlite":
            upsert_sql += (
                " ON CONFLICT(epoch, category, manifest_sha) DO UPDATE SET"
                " build_run_id=excluded.build_run_id,"
                " payload_json=excluded.payload_json,"
                " prepared_at=excluded.prepared_at,"
                " expires_at=excluded.expires_at,"
                " publish_job_name=NULL,"
                " approved_at=NULL,"
                " approved_by=NULL"
            )
        else:
            upsert_sql += (
                " ON DUPLICATE KEY UPDATE"
                " build_run_id=VALUES(build_run_id),"
                " payload_json=VALUES(payload_json),"
                " prepared_at=VALUES(prepared_at),"
                " expires_at=VALUES(expires_at),"
                " publish_job_name=NULL,"
                " approved_at=NULL,"
                " approved_by=NULL"
            )

        event_id = str(uuid.uuid4())
        created_at = _now()
        evidence_json = json.dumps(
            {"run_id": run_id, "expires_at": expires_at},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        def upsert_candidate(cursor):
            cursor.execute(
                upsert_sql,
                (
                    epoch,
                    category,
                    manifest_sha,
                    run_id,
                    payload_json,
                    prepared_at,
                    expires_at,
                ),
            )
            return True

        def operation(cursor):
            return self._transition_with_cursor(
                cursor,
                epoch,
                category,
                manifest_sha,
                status=STATUS_AWAITING_APPROVAL,
                assignments=(
                    f"status={mark}, reason={mark}, run_id={mark}, finished_at=NULL"
                ),
                values=(
                    STATUS_AWAITING_APPROVAL,
                    "post_gate passed; awaiting explicit publish approval",
                    run_id,
                ),
                actor="job_runner",
                source="post_gate_prepared",
                reason="post_gate passed; awaiting explicit publish approval",
                evidence_json=evidence_json,
                event_id=event_id,
                created_at=created_at,
                expected_status=STATUS_RUNNING,
                expected_run_id=run_id,
                before_update=upsert_candidate,
            )

        if not self._transaction(operation):
            raise RuntimeError("ledger changed before publish candidate could be prepared")

    def mark_publish_running(
        self,
        epoch: str,
        category: str,
        manifest_sha: str,
        *,
        build_run_id: str,
        publish_job_name: str,
        approved_by: str,
        approved_at: str,
    ) -> bool:
        mark = self._mark
        update_candidate_sql = (
            f"UPDATE ingest_publish_candidate SET publish_job_name={mark},"
            f" approved_at={mark}, approved_by={mark}"
            f" WHERE epoch={mark} AND category={mark} AND manifest_sha={mark}"
            f" AND build_run_id={mark}"
            " AND publish_job_name IS NULL"
        )
        select_candidate_sql = (
            "SELECT build_run_id, publish_job_name, expires_at FROM ingest_publish_candidate"
            f" WHERE epoch={mark} AND category={mark} AND manifest_sha={mark}"
        )
        if self._dialect == "mysql":
            select_candidate_sql += " FOR UPDATE"
        event_id = str(uuid.uuid4())
        created_at = _now()
        evidence_json = json.dumps(
            {
                "build_run_id": build_run_id,
                "publish_job_name": publish_job_name,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        def reserve_candidate(cursor):
            cursor.execute(select_candidate_sql, (epoch, category, manifest_sha))
            row = cursor.fetchone()
            if row is None:
                return False
            values_row = tuple(row.values()) if isinstance(row, dict) else tuple(row)
            candidate_run_id, existing_job_name, expires_at = values_row
            if (
                str(candidate_run_id) != build_run_id
                or existing_job_name is not None
                or _utc_timestamp(approved_at) > _utc_timestamp(expires_at)
            ):
                return False
            cursor.execute(
                update_candidate_sql,
                (
                    publish_job_name,
                    approved_at,
                    approved_by,
                    epoch,
                    category,
                    manifest_sha,
                    build_run_id,
                ),
            )
            return cursor.rowcount == 1

        def operation(cursor):
            return self._transition_with_cursor(
                cursor,
                epoch,
                category,
                manifest_sha,
                status=STATUS_PUBLISH_RUNNING,
                assignments=f"status={mark}, job_name={mark}, started_at={mark}",
                values=(STATUS_PUBLISH_RUNNING, publish_job_name, approved_at),
                actor=approved_by,
                source="publish_approval",
                reason="exact publish candidate approved",
                evidence_json=evidence_json,
                event_id=event_id,
                created_at=created_at,
                expected_status=STATUS_AWAITING_APPROVAL,
                expected_run_id=build_run_id,
                before_update=reserve_candidate,
            )

        changed = bool(self._transaction(operation))
        if not changed:
            refreshed = self.prepared_candidate(epoch, category, manifest_sha)
            return bool(refreshed and refreshed.publish_job_name == publish_job_name)
        return True

    def restore_awaiting_approval_after_submit_failure(
        self,
        epoch: str,
        category: str,
        manifest_sha: str,
        *,
        build_run_id: str,
        publish_job_name: str,
    ) -> bool:
        mark = self._mark
        candidate_sql = (
            "UPDATE ingest_publish_candidate SET publish_job_name=NULL,"
            " approved_at=NULL, approved_by=NULL"
            f" WHERE epoch={mark} AND category={mark} AND manifest_sha={mark}"
            f" AND build_run_id={mark} AND publish_job_name={mark}"
        )
        event_id = str(uuid.uuid4())
        created_at = _now()
        reason = "publish Job was not created; explicit approval remains retryable"

        def release_candidate(cursor):
            cursor.execute(
                candidate_sql,
                (
                    epoch,
                    category,
                    manifest_sha,
                    build_run_id,
                    publish_job_name,
                ),
            )
            return cursor.rowcount == 1

        def operation(cursor):
            return self._transition_with_cursor(
                cursor,
                epoch,
                category,
                manifest_sha,
                status=STATUS_AWAITING_APPROVAL,
                assignments=(
                    f"status={mark}, reason={mark}, job_name=NULL,"
                    " finished_at=NULL"
                ),
                values=(STATUS_AWAITING_APPROVAL, reason),
                actor="ingest_hook",
                source="publish_submission_failed_retryable",
                reason=reason,
                evidence_json=json.dumps(
                    {"build_run_id": build_run_id},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                event_id=event_id,
                created_at=created_at,
                expected_status=STATUS_PUBLISH_RUNNING,
                expected_job_name=publish_job_name,
                expected_run_id=build_run_id,
                before_update=release_candidate,
            )

        return bool(self._transaction(operation))

    def rearm_failed_candidate(
        self,
        epoch: str,
        category: str,
        manifest_sha: str,
        *,
        build_run_id: str,
        actor: str,
        evidence: dict,
        integrity_updates: dict[str, object] | None = None,
    ) -> bool:
        """Atomically rearm an intact failed publish candidate with an audit event."""
        mark = self._mark
        candidate_sql = (
            "SELECT build_run_id, payload_json FROM ingest_publish_candidate"
            f" WHERE epoch={mark} AND category={mark} AND manifest_sha={mark}"
        )
        if self._dialect == "mysql":
            candidate_sql += " FOR UPDATE"
        reset_sql = (
            "UPDATE ingest_publish_candidate SET publish_job_name=NULL,"
            f" approved_at=NULL, approved_by=NULL, payload_json={mark}"
            f" WHERE epoch={mark} AND category={mark} AND manifest_sha={mark}"
            f" AND build_run_id={mark}"
        )
        event_id = str(uuid.uuid4())
        created_at = _now()
        reason = "audited rearm of intact failed publish candidate"

        def reset_candidate(cursor):
            cursor.execute(candidate_sql, (epoch, category, manifest_sha))
            row = cursor.fetchone()
            if row is None:
                return False
            values_row = tuple(row.values()) if isinstance(row, dict) else tuple(row)
            if str(values_row[0]) != build_run_id:
                return False
            payload = json.loads(str(values_row[1]))
            for key, value in (integrity_updates or {}).items():
                if key in payload and payload[key] != value:
                    return False
                payload[key] = value
            cursor.execute(
                reset_sql,
                (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    epoch, category, manifest_sha, build_run_id,
                ),
            )
            return cursor.rowcount == 1

        def operation(cursor):
            return self._transition_with_cursor(
                cursor,
                epoch,
                category,
                manifest_sha,
                status=STATUS_AWAITING_APPROVAL,
                assignments=(
                    f"status={mark}, reason={mark}, job_name=NULL, finished_at=NULL"
                ),
                values=(STATUS_AWAITING_APPROVAL, reason),
                actor=actor,
                source="audited_publish_rearm",
                reason=reason,
                evidence_json=json.dumps(
                    evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                event_id=event_id,
                created_at=created_at,
                expected_status=STATUS_FAILED,
                expected_run_id=build_run_id,
                before_update=reset_candidate,
            )

        return bool(self._transaction(operation))

    def mark_publish_candidate_expired(
        self,
        epoch: str,
        category: str,
        manifest_sha: str,
        *,
        build_run_id: str,
        actor: str,
    ) -> bool:
        reason = "publish candidate expired before approval"
        return self._transition(
            epoch,
            category,
            manifest_sha,
            status=STATUS_FAILED,
            assignments=f"status={self._mark}, reason={self._mark}, finished_at={self._mark}",
            values=(STATUS_FAILED, reason, _now()),
            actor=actor,
            source="publish_candidate_expired",
            reason=reason,
            evidence={"build_run_id": build_run_id},
            expected_status=STATUS_AWAITING_APPROVAL,
            expected_run_id=build_run_id,
        )

    def reconcile_terminal(
        self,
        epoch: str,
        category: str,
        manifest_sha: str,
        *,
        status: str,
        reason: str,
        actor: str,
        source: str,
        evidence: dict,
        expected_job_name: str | None = None,
        expected_run_id: str | None = None,
        expected_status: str = STATUS_RUNNING,
    ) -> bool:
        if status not in (STATUS_COMPLETE, STATUS_FAILED):
            raise ValueError(f"terminal reconciliation requires complete/failed, got {status!r}")
        return self._transition(
            epoch,
            category,
            manifest_sha,
            status=status,
            assignments=f"status={self._mark}, reason={self._mark}, finished_at={self._mark}",
            values=(status, reason[:4000], _now()),
            actor=actor,
            source=source,
            reason=reason,
            evidence=evidence,
            expected_status=expected_status,
            expected_job_name=expected_job_name,
            expected_run_id=expected_run_id,
        )

    # -- reads ----------------------------------------------------------------
    def status(self, epoch: str, category: str, manifest_sha: str) -> LedgerEntry | None:
        row = self._fetch_row(epoch, category, manifest_sha)
        return self._entry(row) if row is not None else None

    def prepared_candidate(
        self, epoch: str, category: str, manifest_sha: str
    ) -> PreparedCandidate | None:
        cursor = self._execute(
            "SELECT epoch, category, manifest_sha, build_run_id, publish_job_name,"
            " payload_json, prepared_at, expires_at, approved_at, approved_by"
            " FROM ingest_publish_candidate"
            " WHERE epoch=? AND category=? AND manifest_sha=?",
            (epoch, category, manifest_sha),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._prepared_candidate(row)

    def awaiting_publish_candidates(
        self, category: str | None = None
    ) -> list[PreparedCandidate]:
        category_clause = ""
        params: tuple[str, ...] = (STATUS_AWAITING_APPROVAL,)
        if category is not None:
            category_clause = f" AND candidate.category={self._mark}"
            params += (category,)
        cursor = self._execute(
            "SELECT candidate.epoch, candidate.category, candidate.manifest_sha,"
            " candidate.build_run_id, candidate.publish_job_name,"
            " candidate.payload_json, candidate.prepared_at, candidate.expires_at,"
            " candidate.approved_at, candidate.approved_by"
            " FROM ingest_publish_candidate AS candidate"
            " INNER JOIN ingest_ledger AS ledger"
            " ON ledger.epoch=candidate.epoch"
            " AND ledger.category=candidate.category"
            " AND ledger.manifest_sha=candidate.manifest_sha"
            f" WHERE ledger.status={self._mark}{category_clause}"
            " ORDER BY candidate.prepared_at, candidate.epoch",
            params,
        )
        return [self._prepared_candidate(row) for row in cursor.fetchall()]

    @staticmethod
    def _prepared_candidate(row) -> PreparedCandidate:
        values = tuple(row.values()) if isinstance(row, dict) else tuple(row)
        return PreparedCandidate(
            epoch=str(values[0]),
            category=str(values[1]),
            manifest_sha=str(values[2]),
            build_run_id=str(values[3]),
            publish_job_name=str(values[4]) if values[4] else None,
            payload=json.loads(values[5]),
            prepared_at=str(values[6]),
            expires_at=str(values[7]),
            approved_at=str(values[8]) if values[8] else None,
            approved_by=str(values[9]) if values[9] else None,
        )

    def status_transitions(
        self, epoch: str, category: str, manifest_sha: str
    ) -> list[StatusTransition]:
        cursor = self._execute(
            "SELECT event_id, previous_status, status, actor, source, reason,"
            " job_name, evidence_json, created_at FROM ingest_status_transition"
            " WHERE epoch=? AND category=? AND manifest_sha=? ORDER BY id",
            (epoch, category, manifest_sha),
        )
        result = []
        for row in cursor.fetchall():
            values = tuple(row.values()) if isinstance(row, dict) else tuple(row)
            result.append(
                StatusTransition(
                    event_id=str(values[0]),
                    previous_status=str(values[1]) if values[1] else None,
                    status=str(values[2]),
                    actor=str(values[3]),
                    source=str(values[4]),
                    reason=str(values[5]) if values[5] else None,
                    job_name=str(values[6]) if values[6] else None,
                    evidence=json.loads(values[7]),
                    created_at=str(values[8]),
                )
            )
        return result

    def previous_complete_total(self, category: str, *, before_epoch: str) -> int | None:
        """Total loaded rows of the most recent completed submission before this epoch."""
        cursor = self._execute(
            "SELECT row_counts FROM ingest_ledger WHERE category=? AND status=? AND epoch<?"
            " ORDER BY epoch DESC, id DESC LIMIT 1",
            (category, STATUS_COMPLETE, before_epoch),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        raw = tuple(row.values())[0] if isinstance(row, dict) else row[0]
        if not raw:
            return None
        return sum(json.loads(raw).values())

    # -- stage observation (best-effort; never fails the load) ---------------
    def record_stage(
        self,
        epoch: str,
        category: str,
        manifest_sha: str,
        *,
        run_id: str,
        seq: int,
        stage: str,
        status: str,
        reason: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Upsert one stage row by (identity, run_id, seq). Best-effort by design:

        observation must never break the load (S-4). Any DB error is swallowed with
        an stderr note; the run continues. Rows accumulate per run_id — a retry uses
        a new run_id and never overwrites a prior attempt's history (S-3).
        """
        try:
            existing = self._execute(
                "SELECT id FROM ingest_stage_event"
                " WHERE epoch=? AND category=? AND manifest_sha=? AND run_id=? AND seq=?",
                (epoch, category, manifest_sha, run_id, seq),
            ).fetchone()
            if existing is None:
                self._execute(
                    "INSERT INTO ingest_stage_event"
                    " (epoch, category, manifest_sha, run_id, seq, stage, status, reason,"
                    "  started_at, finished_at, duration_ms)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (epoch, category, manifest_sha, run_id, seq, stage, status, reason,
                     started_at, finished_at, duration_ms),
                )
            else:
                self._execute(
                    "UPDATE ingest_stage_event SET stage=?, status=?, reason=?,"
                    " finished_at=COALESCE(?, finished_at), duration_ms=COALESCE(?, duration_ms),"
                    " started_at=COALESCE(started_at, ?)"
                    " WHERE epoch=? AND category=? AND manifest_sha=? AND run_id=? AND seq=?",
                    (stage, status, reason, finished_at, duration_ms, started_at,
                     epoch, category, manifest_sha, run_id, seq),
                )
        except Exception as exc:  # noqa: BLE001 — observation is best-effort (S-4)
            import sys
            print(f"[stage] record failed (ignored; load continues): {type(exc).__name__}: {exc}",
                  file=sys.stderr)

    def stage_events(self, epoch: str, category: str, manifest_sha: str) -> list[StageEvent]:
        cursor = self._execute(
            "SELECT run_id, seq, stage, status, reason, started_at, finished_at, duration_ms"
            " FROM ingest_stage_event WHERE epoch=? AND category=? AND manifest_sha=?"
            " ORDER BY run_id, seq",
            (epoch, category, manifest_sha),
        )
        events: list[StageEvent] = []
        for row in cursor.fetchall():
            values = tuple(row.values()) if isinstance(row, dict) else tuple(row)
            events.append(StageEvent(
                run_id=str(values[0]), seq=int(values[1]), stage=str(values[2]),
                status=str(values[3]), reason=values[4],
                started_at=str(values[5]) if values[5] else None,
                finished_at=str(values[6]) if values[6] else None,
                duration_ms=int(values[7]) if values[7] is not None else None,
            ))
        return events

    def agent_refresh_status(self, category: str) -> dict[str, str | None]:
        latest_ingest = self._execute(
            "SELECT epoch, manifest_sha FROM ingest_ledger"
            " WHERE category=? AND status='complete'"
            " ORDER BY finished_at DESC, id DESC LIMIT 1",
            (category,),
        ).fetchone()
        latest_agent = self._execute(
            "SELECT epoch, manifest_sha, status, finished_at FROM ingest_stage_event"
            " WHERE category=? AND stage='agent_refresh'"
            " ORDER BY id DESC LIMIT 1",
            (category,),
        ).fetchone()
        last_success = self._execute(
            "SELECT finished_at FROM ingest_stage_event"
            " WHERE category=? AND stage='agent_refresh' AND status='complete'"
            " ORDER BY finished_at DESC, id DESC LIMIT 1",
            (category,),
        ).fetchone()

        def values(row):
            return tuple(row.values()) if isinstance(row, dict) else tuple(row)

        ingest_identity = (
            (str(values(latest_ingest)[0]), str(values(latest_ingest)[1]))
            if latest_ingest
            else None
        )
        success_at = str(values(last_success)[0]) if last_success else None
        if latest_agent is None:
            return {
                "agent_epoch": None,
                "agent_status": "stale" if ingest_identity else "unknown",
                "last_success_at": success_at,
            }
        agent_epoch, agent_manifest_sha, status, _finished_at = values(latest_agent)
        agent_epoch = str(agent_epoch)
        if str(status) == "failed":
            agent_status = "failed"
        elif (
            str(status) == "complete"
            and (agent_epoch, str(agent_manifest_sha)) == ingest_identity
        ):
            agent_status = "fresh"
        else:
            agent_status = "stale"
        return {
            "agent_epoch": agent_epoch,
            "agent_status": agent_status,
            "last_success_at": success_at,
        }

    # -- completion signal observation --------------------------------------
    def record_signal(
        self, epoch: str, category: str, manifest_sha: str, *, run_id: str,
        event: str, mode: str, rows_loaded: int, delivery_status: str,
        attempts: int, reason: str | None, payload: dict,
    ) -> None:
        """Persist delivery state without permitting identity/count drift."""
        prior_identity = self._execute(
            "SELECT rows_loaded FROM ingest_signal_event"
            " WHERE epoch=? AND category=? AND manifest_sha=? ORDER BY id LIMIT 1",
            (epoch, category, manifest_sha),
        ).fetchone()
        if prior_identity is not None:
            prior = int(
                tuple(prior_identity.values())[0]
                if isinstance(prior_identity, dict)
                else prior_identity[0]
            )
            if prior != rows_loaded:
                raise ValueError(
                    f"signal identity count drift: prior rows_loaded={prior}, new={rows_loaded}"
                )

        existing = self._execute(
            "SELECT id FROM ingest_signal_event"
            " WHERE epoch=? AND category=? AND manifest_sha=? AND event=?",
            (epoch, category, manifest_sha, event),
        ).fetchone()
        if existing is not None:
            self._execute(
                "UPDATE ingest_signal_event SET run_id=?, mode=?, delivery_status=?, attempts=?,"
                " reason=?, payload_json=?, created_at=?"
                " WHERE epoch=? AND category=? AND manifest_sha=? AND event=?",
                (run_id, mode, delivery_status, attempts, reason,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), _now(),
                 epoch, category, manifest_sha, event),
            )
            return
        self._execute(
            "INSERT INTO ingest_signal_event"
            " (epoch, category, manifest_sha, run_id, event, mode, rows_loaded,"
            " delivery_status, attempts, reason, payload_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (epoch, category, manifest_sha, run_id, event, mode, rows_loaded,
             delivery_status, attempts, reason,
             json.dumps(payload, ensure_ascii=False, sort_keys=True), _now()),
        )

    def signal_events(self, epoch: str, category: str, manifest_sha: str) -> list[SignalEvent]:
        cursor = self._execute(
            "SELECT run_id, event, mode, rows_loaded, delivery_status, attempts, reason,"
            " payload_json, created_at FROM ingest_signal_event"
            " WHERE epoch=? AND category=? AND manifest_sha=? ORDER BY id",
            (epoch, category, manifest_sha),
        )
        result = []
        for row in cursor.fetchall():
            values = tuple(row.values()) if isinstance(row, dict) else tuple(row)
            result.append(SignalEvent(
                run_id=str(values[0]), event=str(values[1]), mode=str(values[2]),
                rows_loaded=int(values[3]), delivery_status=str(values[4]),
                attempts=int(values[5]), reason=values[6], payload=json.loads(values[7]),
                created_at=str(values[8]),
            ))
        return result


def open_sqlite_ledger(path: Path) -> Ledger:
    path.parent.mkdir(parents=True, exist_ok=True)
    # The trigger service handles requests on a threadpool; sqlite rehearsal
    # ledgers must therefore allow cross-thread use (writes stay serialised
    # by sqlite's own locking).
    conn = sqlite3.connect(str(path), check_same_thread=False)
    ledger = Ledger(conn, dialect="sqlite")
    ledger.ensure_table()
    return ledger
