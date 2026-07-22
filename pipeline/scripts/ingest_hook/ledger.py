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
_HELD_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_COMPLETE)

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
  status        VARCHAR(16)  NOT NULL,
  reason        TEXT         NULL,
  job_name      VARCHAR(128) NULL,
  run_id        VARCHAR(64)  NULL,
  row_counts    TEXT         NULL,
  received_at   DATETIME     NOT NULL,
  started_at    DATETIME     NULL,
  finished_at   DATETIME     NULL,
  UNIQUE KEY uq_ledger_identity (epoch, category, manifest_sha),
  KEY idx_ledger_category_status (category, status)
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
  status        VARCHAR(16)  NOT NULL,
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


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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
        self._lock = threading.Lock()

    # -- schema ------------------------------------------------------------
    def ensure_table(self) -> None:
        cursor = self._conn.cursor()
        cursor.execute(_DDL_SQLITE if self._dialect == "sqlite" else _DDL_MYSQL)
        cursor.execute(_DDL_STAGE_SQLITE if self._dialect == "sqlite" else _DDL_STAGE_MYSQL)
        cursor.execute(_DDL_SIGNAL_SQLITE if self._dialect == "sqlite" else _DDL_SIGNAL_MYSQL)
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
        existing = self._fetch_row(epoch, category, manifest_sha)
        if existing is not None:
            status = self._entry(existing).status
            if status in _HELD_STATUSES:
                return ReceiveDecision("noop", status, f"identity already {status}; webhook ignored")
            # failed -> allow retry
            self._execute(
                "UPDATE ingest_ledger SET status=?, reason=?, received_at=?, uploaded_by=?"
                " WHERE epoch=? AND category=? AND manifest_sha=?",
                (STATUS_QUEUED, "re-queued after failure", _now(), uploaded_by, epoch, category, manifest_sha),
            )
            return ReceiveDecision("queued", STATUS_QUEUED, "previous attempt failed; re-queued")
        self._execute(
            "INSERT INTO ingest_ledger"
            " (epoch, category, manifest_sha, manifest_path, uploaded_by, status, received_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (epoch, category, manifest_sha, manifest_path, uploaded_by, STATUS_QUEUED, _now()),
        )
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

    # -- state transitions ---------------------------------------------------
    def mark_running(self, epoch: str, category: str, manifest_sha: str, *, job_name: str, run_id: str) -> None:
        self._execute(
            "UPDATE ingest_ledger SET status=?, job_name=?, run_id=?, started_at=?"
            " WHERE epoch=? AND category=? AND manifest_sha=?",
            (STATUS_RUNNING, job_name, run_id, _now(), epoch, category, manifest_sha),
        )

    def mark_complete(self, epoch: str, category: str, manifest_sha: str, *, row_counts: dict[str, int]) -> None:
        self._execute(
            "UPDATE ingest_ledger SET status=?, reason=NULL, row_counts=?, finished_at=?"
            " WHERE epoch=? AND category=? AND manifest_sha=?",
            (STATUS_COMPLETE, json.dumps(row_counts, ensure_ascii=False), _now(), epoch, category, manifest_sha),
        )

    def mark_failed(self, epoch: str, category: str, manifest_sha: str, *, reason: str) -> None:
        self._execute(
            "UPDATE ingest_ledger SET status=?, reason=?, finished_at=?"
            " WHERE epoch=? AND category=? AND manifest_sha=?",
            (STATUS_FAILED, reason[:4000], _now(), epoch, category, manifest_sha),
        )

    def mark_gate_failed(self, epoch: str, category: str, manifest_sha: str, *, reason: str) -> None:
        self._execute(
            "UPDATE ingest_ledger SET status=?, reason=?, finished_at=?"
            " WHERE epoch=? AND category=? AND manifest_sha=?",
            (STATUS_GATE_FAILED, reason[:4000], _now(), epoch, category, manifest_sha),
        )

    # -- reads ----------------------------------------------------------------
    def status(self, epoch: str, category: str, manifest_sha: str) -> LedgerEntry | None:
        row = self._fetch_row(epoch, category, manifest_sha)
        return self._entry(row) if row is not None else None

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
