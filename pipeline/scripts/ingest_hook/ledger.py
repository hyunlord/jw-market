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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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


class Ledger:
    """Dialect-neutral ledger operations over an injected DB-API connection."""

    def __init__(self, conn, dialect: str = "sqlite"):
        if dialect not in ("sqlite", "mysql"):
            raise ValueError(f"unknown dialect {dialect!r}")
        self._conn = conn
        self._dialect = dialect
        self._mark = "?" if dialect == "sqlite" else "%s"

    # -- schema ------------------------------------------------------------
    def ensure_table(self) -> None:
        cursor = self._conn.cursor()
        cursor.execute(_DDL_SQLITE if self._dialect == "sqlite" else _DDL_MYSQL)
        self._conn.commit()

    # -- helpers -----------------------------------------------------------
    def _execute(self, sql: str, params: tuple = ()):
        cursor = self._conn.cursor()
        cursor.execute(sql.replace("?", self._mark) if self._dialect == "mysql" else sql, params)
        self._conn.commit()
        return cursor

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


def open_sqlite_ledger(path: Path) -> Ledger:
    path.parent.mkdir(parents=True, exist_ok=True)
    # The trigger service handles requests on a threadpool; sqlite rehearsal
    # ledgers must therefore allow cross-thread use (writes stay serialised
    # by sqlite's own locking).
    conn = sqlite3.connect(str(path), check_same_thread=False)
    ledger = Ledger(conn, dialect="sqlite")
    ledger.ensure_table()
    return ledger
