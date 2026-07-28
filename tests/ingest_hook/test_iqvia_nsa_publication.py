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
    def __init__(self, *, fail_provenance: bool = False) -> None:
        self.fail_provenance = fail_provenance
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False

    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> None:
        self.statements.append((statement, parameters))
        if self.fail_provenance and statement.startswith("INSERT INTO") and (
            publication.PROVENANCE_TABLE in statement
        ):
            raise RuntimeError("provenance denied")

    def fetchone(self) -> tuple[int]:
        return (7,)

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self, *, fail_provenance: bool = False) -> None:
        self.cursor_value = _Cursor(fail_provenance=fail_provenance)
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
