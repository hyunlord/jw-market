from __future__ import annotations

from copy import deepcopy
from typing import Any

import pymysql
import pytest

from pipeline.etl.io.mart.filter_dimension_copy import (
    copy_table_consistent_snapshot,
)


_COLUMNS = ("id", "value")
_SOURCE = "`mart`.`live`"
_TARGET = "`mart`.`backup`"


class _SnapshotCursor:
    def __init__(self, conn: "_SnapshotConnection") -> None:
        self._conn = conn
        self._rows: list[dict[str, Any]] = []

    def __enter__(self) -> "_SnapshotCursor":
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(sql.split())
        self._conn.calls.append((normalized, params))
        if normalized.startswith("SHOW COLUMNS FROM"):
            self._rows = [{"Field": column} for column in _COLUMNS]
            return
        if normalized.startswith("SELECT") and f"FROM {_SOURCE}" in normalized:
            after_id = int((params or (0,))[0])
            rows = [row for row in self._conn.snapshot_rows if row["id"] > after_id]
            self._rows = deepcopy(rows[: self._conn.batch_size])
            self._conn.source_batches += 1
            if (
                self._conn.source_batches == 1
                and self._conn.on_first_source_batch is not None
            ):
                self._conn.on_first_source_batch()
            return
        self._rows = []

    def fetchall(self) -> list[dict[str, Any]]:
        return deepcopy(self._rows)


class _SnapshotConnection:
    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        batch_size: int,
        on_first_source_batch: Any = None,
    ) -> None:
        self.snapshot_rows = deepcopy(rows)
        self.batch_size = batch_size
        self.on_first_source_batch = on_first_source_batch
        self.source_batches = 0
        self.calls: list[tuple[str, tuple[Any, ...] | None]] = []
        self.rollbacks = 0

    def cursor(self) -> _SnapshotCursor:
        return _SnapshotCursor(self)

    def rollback(self) -> None:
        self.rollbacks += 1


class _WriterCursor:
    def __init__(self, conn: "_WriterConnection") -> None:
        self._conn = conn
        self._rows: list[dict[str, Any]] = []
        self.rowcount = 0

    def __enter__(self) -> "_WriterCursor":
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(sql.split())
        self._conn.calls.append((normalized, params))
        self.rowcount = 0
        if normalized.startswith("SHOW COLUMNS FROM"):
            self._rows = [{"Field": column} for column in _COLUMNS]
        elif normalized.startswith("SELECT") and f"FROM {_TARGET}" in normalized:
            after_id = int((params or (0,))[0])
            rows = [row for row in self._conn.target_rows if row["id"] > after_id]
            self._rows = deepcopy(rows[: self._conn.batch_size])
        elif normalized.startswith(f"DROP TABLE IF EXISTS {_TARGET}"):
            self._conn.target_exists = False
            self._conn.target_rows.clear()
            self._rows = []
        else:
            self._rows = []

    def executemany(self, sql: str, params: list[tuple[Any, ...]]) -> None:
        normalized = " ".join(sql.split())
        self._conn.calls.append((normalized, tuple(params)))
        self._conn.batch_number += 1
        if self._conn.batch_number == self._conn.fail_batch:
            raise pymysql.OperationalError(1180, "injected batch failure")
        self._conn.target_rows.extend(
            dict(zip(_COLUMNS, values, strict=True)) for values in params
        )
        self.rowcount = len(params)

    def fetchall(self) -> list[dict[str, Any]]:
        return deepcopy(self._rows)


class _WriterConnection:
    def __init__(
        self,
        live_rows: list[dict[str, Any]],
        *,
        batch_size: int,
        fail_batch: int | None = None,
        corrupt_before_verify: bool = False,
    ) -> None:
        self.live_rows = deepcopy(live_rows)
        self.target_rows: list[dict[str, Any]] = []
        self.target_exists = True
        self.batch_size = batch_size
        self.fail_batch = fail_batch
        self.corrupt_before_verify = corrupt_before_verify
        self.batch_number = 0
        self.commits = 0
        self.rollbacks = 0
        self.calls: list[tuple[str, Any]] = []

    def cursor(self) -> _WriterCursor:
        if (
            self.corrupt_before_verify
            and self.batch_number > 0
            and len(self.target_rows) == 3
        ):
            self.target_rows[-1]["value"] = "CORRUPTED"
            self.corrupt_before_verify = False
        return _WriterCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _rows() -> list[dict[str, Any]]:
    return [
        {"id": 1, "value": "alpha"},
        {"id": 2, "value": "beta"},
        {"id": 3, "value": "gamma"},
    ]


def test_consistent_snapshot_copy_proves_row_count_and_sha256() -> None:
    rows = _rows()
    snapshot = _SnapshotConnection(rows, batch_size=2)
    writer = _WriterConnection(rows, batch_size=2)

    proof = copy_table_consistent_snapshot(
        snapshot,
        writer,
        source_table=_SOURCE,
        target_table=_TARGET,
        batch_size=2,
    )

    assert proof.row_count == 3
    assert proof.source_sha256 == proof.target_sha256
    assert writer.target_rows == rows
    assert writer.target_exists is True
    assert writer.commits == 2
    assert snapshot.rollbacks == 1


def test_consistent_snapshot_reports_each_batch_and_ready_identity() -> None:
    rows = _rows()
    snapshot = _SnapshotConnection(rows, batch_size=2)
    writer = _WriterConnection(rows, batch_size=2)
    events: list[tuple[str, dict[str, Any]]] = []

    proof = copy_table_consistent_snapshot(
        snapshot,
        writer,
        source_table=_SOURCE,
        target_table=_TARGET,
        batch_size=2,
        on_progress=lambda event, details: events.append((event, details)),
    )

    assert [event for event, _details in events] == [
        "candidate_copy_batch",
        "candidate_copy_batch",
        "candidate_ready",
    ]
    assert [details["rows_affected"] for _event, details in events[:2]] == [2, 1]
    assert events[-1][1]["pre_live_rows"] == 3
    assert events[-1][1]["baseline_source_sha256"] == proof.source_sha256


def test_candidate_ready_journal_failure_discards_verified_candidate() -> None:
    rows = _rows()
    snapshot = _SnapshotConnection(rows, batch_size=2)
    writer = _WriterConnection(rows, batch_size=2)

    def fail_ready(event: str, _details: dict[str, Any]) -> None:
        if event == "candidate_ready":
            raise RuntimeError("injected journal failure")

    with pytest.raises(RuntimeError, match="partial target removed"):
        copy_table_consistent_snapshot(
            snapshot,
            writer,
            source_table=_SOURCE,
            target_table=_TARGET,
            batch_size=2,
            on_progress=fail_ready,
        )

    assert writer.target_exists is False
    assert writer.target_rows == []


def test_mid_batch_failure_removes_partial_backup_and_preserves_live() -> None:
    rows = _rows()
    snapshot = _SnapshotConnection(rows, batch_size=2)
    writer = _WriterConnection(rows, batch_size=2, fail_batch=2)
    live_before = deepcopy(writer.live_rows)

    with pytest.raises(RuntimeError, match="consistent snapshot copy failed"):
        copy_table_consistent_snapshot(
            snapshot,
            writer,
            source_table=_SOURCE,
            target_table=_TARGET,
            batch_size=2,
        )

    assert writer.live_rows == live_before
    assert writer.target_exists is False
    assert writer.target_rows == []
    assert writer.rollbacks == 1
    assert snapshot.rollbacks == 1


def test_checksum_mismatch_invalidates_backup_and_preserves_live() -> None:
    rows = _rows()
    snapshot = _SnapshotConnection(rows, batch_size=2)
    writer = _WriterConnection(rows, batch_size=2, corrupt_before_verify=True)
    live_before = deepcopy(writer.live_rows)

    with pytest.raises(RuntimeError, match="integrity mismatch"):
        copy_table_consistent_snapshot(
            snapshot,
            writer,
            source_table=_SOURCE,
            target_table=_TARGET,
            batch_size=2,
        )

    assert writer.live_rows == live_before
    assert writer.target_exists is False
    assert writer.target_rows == []


def test_concurrent_writer_does_not_mix_snapshots_into_backup() -> None:
    rows = _rows()
    writer = _WriterConnection(rows, batch_size=2)

    def mutate_live_after_snapshot_started() -> None:
        writer.live_rows[0]["value"] = "new-alpha"
        writer.live_rows.append({"id": 4, "value": "delta"})

    snapshot = _SnapshotConnection(
        rows,
        batch_size=2,
        on_first_source_batch=mutate_live_after_snapshot_started,
    )

    proof = copy_table_consistent_snapshot(
        snapshot,
        writer,
        source_table=_SOURCE,
        target_table=_TARGET,
        batch_size=2,
    )

    assert writer.live_rows != rows
    assert writer.target_rows == rows
    assert proof.row_count == 3
    assert proof.source_sha256 == proof.target_sha256
    statements = [sql for sql, _params in snapshot.calls]
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ" in statements
    assert "START TRANSACTION WITH CONSISTENT SNAPSHOT" in statements
