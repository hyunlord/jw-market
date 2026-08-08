from __future__ import annotations

from dataclasses import dataclass

import pytest

from pipeline.scripts.ingest_hook import iqvia_nsa_publication as publication


@dataclass(frozen=True)
class _File:
    path: str
    sha256: str
    rows: int | None = None


@dataclass(frozen=True)
class _Config:
    target_db: str = "jw_mart_d2"
    builder_commit: str = "a" * 40
    image_ref: str = "registry/jw-market@sha256:" + ("b" * 64)


class _Cursor:
    def __init__(self, *, fail_provenance: bool = False, rows: list[object] | None = None) -> None:
        self.fail_provenance = fail_provenance
        self.rows = list(rows or [])
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False
        self.rowcount = 0

    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> None:
        self.statements.append((statement, parameters))
        self.rowcount = 1 if statement.startswith("UPDATE") else 0
        if self.fail_provenance and statement.startswith("INSERT INTO") and (
            publication.PROVENANCE_TABLE in statement
        ):
            raise RuntimeError("provenance denied")

    def fetchone(self) -> object:
        return self.rows.pop(0) if self.rows else (7,)

    def fetchall(self) -> list[object]:
        rows, self.rows = self.rows, []
        return rows

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(
        self,
        *,
        fail_provenance: bool = False,
        rows: list[object] | None = None,
    ) -> None:
        self.cursor_value = _Cursor(fail_provenance=fail_provenance, rows=rows)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        return self.cursor_value

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _evidence() -> publication.PublicationEvidence:
    return publication.build_publication_evidence(
        (
            _File("b.xlsx", "b" * 64, 2),
            _File("a.xlsx", "a" * 64, 1),
        ),
        {"a.xlsx": 10, "b.xlsx": 20},
        ("2026Q1", "2021Q2"),
    )


def test_evidence_is_order_independent_and_records_full_inventory() -> None:
    first = _evidence()
    second = publication.build_publication_evidence(
        (
            _File("a.xlsx", "a" * 64, 1),
            _File("b.xlsx", "b" * 64, 2),
        ),
        {"b.xlsx": 20, "a.xlsx": 10},
        ("2021Q2", "2026Q1"),
    )

    assert first == second
    assert first.window_start == "2021Q2"
    assert first.window_end == "2026Q1"
    assert '"rows":10' in first.inventory_json


def test_provenance_record_advances_epoch_and_writes_image_identity() -> None:
    connection = _Connection()

    epoch = publication.record_publication_provenance(
        connection,
        _Config(),
        run_id="run1",
        epoch="2026Q1",
        evidence=_evidence(),
    )

    assert epoch == 8
    insert = next(
        parameters
        for statement, parameters in connection.cursor_value.statements
        if statement.startswith("INSERT INTO") and publication.PROVENANCE_TABLE in statement
    )
    assert _Config.builder_commit in insert
    assert _Config.image_ref in insert
    assert connection.commits == 2
    assert connection.rollbacks == 0


def test_provenance_record_failure_is_not_published() -> None:
    connection = _Connection(fail_provenance=True)

    with pytest.raises(RuntimeError, match="provenance denied"):
        publication.record_publication_provenance(
            connection,
            _Config(),
            run_id="run1",
            epoch="2026Q1",
            evidence=_evidence(),
        )

    assert connection.rollbacks == 1


def test_rolled_back_publication_is_read_by_exact_run_id() -> None:
    row = {
        "mart_publication_epoch": 12,
        "category": "iqvia_nsa",
        "epoch": "2026-Q1",
        "run_id": "20260808182426423756",
        "input_inventory_sha256": "d" * 64,
        "input_inventory_json": '[{"path":"nsa.xlsx","rows":891567}]',
        "builder_commit": "a" * 40,
        "image_digest": "registry/image@sha256:" + ("b" * 64),
        "window_start": "2021-Q2",
        "window_end": "2026-Q1",
        "published_at_utc": "2026-08-08T20:31:00+00:00",
        "status": "rolled_back",
    }
    connection = _Connection(rows=[row])

    result = publication.read_rolled_back_publication(
        connection,
        _Config(),
        run_id="20260808182426423756",
    )

    assert result.run_id == "20260808182426423756"
    assert result.inventory_json == row["input_inventory_json"]
    select = connection.cursor_value.statements[0]
    assert "run_id=%s AND category='iqvia_nsa' AND status='rolled_back'" in select[0]
    assert select[1] == ("20260808182426423756",)


def test_rolled_back_publication_rejects_duplicate_run_id_rows() -> None:
    row = {
        "mart_publication_epoch": 12,
        "category": "iqvia_nsa",
        "epoch": "2026-Q1",
        "run_id": "20260808182426423756",
        "input_inventory_sha256": "d" * 64,
        "input_inventory_json": "[]",
        "builder_commit": "a" * 40,
        "image_digest": "image",
        "window_start": "2021-Q2",
        "window_end": "2026-Q1",
        "published_at_utc": "2026-08-08T20:31:00+00:00",
    }
    connection = _Connection(rows=[row, row])

    with pytest.raises(RuntimeError, match="expected exactly 1 row, found 2"):
        publication.read_rolled_back_publication(
            connection,
            _Config(),
            run_id="20260808182426423756",
        )


def test_recovered_publication_uses_rolled_back_compare_and_swap() -> None:
    connection = _Connection()

    publication.mark_publication_recovered(
        connection,
        _Config(),
        run_id="20260808182426423756",
        publication_epoch=12,
        inventory_sha256="d" * 64,
    )

    update = next(
        item
        for item in connection.cursor_value.statements
        if item[0].startswith("UPDATE")
    )
    assert "status='rolled_back'" in update[0]
    assert "status='published'" in update[0]
    assert "mart_publication_epoch=%s" in update[0]
    assert "input_inventory_sha256=%s" in update[0]
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_recovered_publication_rejects_lost_compare_and_swap() -> None:
    connection = _Connection()
    original_execute = connection.cursor_value.execute

    def no_match(statement: str, parameters: tuple[object, ...] = ()) -> None:
        original_execute(statement, parameters)
        if statement.startswith("UPDATE"):
            connection.cursor_value.rowcount = 0

    connection.cursor_value.execute = no_match  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="CAS matched 0 rows"):
        publication.mark_publication_recovered(
            connection,
            _Config(),
            run_id="20260808182426423756",
            publication_epoch=12,
            inventory_sha256="d" * 64,
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_failed_recovery_returns_publication_to_rolled_back_with_cas() -> None:
    connection = _Connection()

    publication.mark_publication_recovery_rolled_back(
        connection,
        _Config(),
        run_id="20260808182426423756",
        publication_epoch=12,
        inventory_sha256="d" * 64,
    )

    update = next(
        item
        for item in connection.cursor_value.statements
        if item[0].startswith("UPDATE")
    )
    assert "status='published'" in update[0]
    assert "status='rolled_back'" in update[0]
    assert connection.commits == 1
