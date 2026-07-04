from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.scripts.deploy import audit_code_matrix_migration as migration


def test_batch_size_is_capped_for_galera() -> None:
    assert migration.bounded_batch_size(500) == 200
    assert migration.bounded_batch_size(37) == 37


def test_update_touches_only_audit_code_matrix_in_bounded_batches() -> None:
    batches: list[list[tuple[object, ...]]] = []
    statements: list[str] = []

    class Cursor:
        def __enter__(self) -> "Cursor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def executemany(self, sql: str, payload: list[tuple[object, ...]]) -> None:
            statements.append(sql)
            batches.append(payload)

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    updates = [
        migration.AuditMatrixUpdate(
            brand_key=f"brand-{idx}",
            atc4_code="C10A1",
            measure="sales",
            audit_code_matrix='{"KPA":{"2025-Q4":1}}',
        )
        for idx in range(205)
    ]

    count = migration.update_audit_code_matrices(Connection(), "jw_mart_d2", updates, batch_size=999)

    assert count == 205
    assert [len(batch) for batch in batches] == [200, 5]
    assert set(statements) == {
        "UPDATE `jw_mart_d2`.`mart_general_brand_metric` "
        "SET audit_code_matrix = %s "
        "WHERE source = %s AND brand_key = %s AND atc4_code = %s AND measure = %s"
    }


def test_ensure_column_is_idempotent() -> None:
    executed: list[tuple[str, object]] = []

    class Cursor:
        def __enter__(self) -> "Cursor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, sql: str, params: object = None) -> None:
            executed.append((sql, params))

        def fetchone(self) -> dict[str, int]:
            return {"column_count": 1}

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    assert migration.ensure_audit_code_matrix_column(Connection(), "jw_mart_d2") is False
    assert len(executed) == 1
    assert "information_schema.COLUMNS" in executed[0][0]


def test_build_update_plan_uses_canonical_general_builder(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_compute_general(**kwargs: object) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
        captured.update(kwargs)
        return (
            [
                {
                    "brand_key": "리바로",
                    "atc4_code": "C10A1",
                    "measure": "sales",
                    "audit_code_matrix": {"KPA": {"2025-Q4": 10.0}},
                },
                {
                    "brand_key": "리바로",
                    "atc4_code": "C10A1",
                    "measure": "unit",
                    "audit_code_matrix": {},
                },
            ],
            [],
            {"source": "iqvia_nsa"},
        )

    monkeypatch.setattr(migration, "compute_general", fake_compute_general)

    updates, stats = migration.build_update_plan(limit_atc4=1, max_rows=500, output_dir=tmp_path)

    assert captured == {
        "source": "iqvia_nsa",
        "dry_run": True,
        "insert": False,
        "limit_atc4": 1,
        "max_rows": 500,
        "output_dir": tmp_path,
    }
    assert stats == {"source": "iqvia_nsa"}
    assert updates == [
        migration.AuditMatrixUpdate("리바로", "C10A1", "sales", '{"KPA":{"2025-Q4":10.0}}'),
        migration.AuditMatrixUpdate("리바로", "C10A1", "unit", None),
    ]


def test_protected_fingerprint_excludes_audit_code_matrix() -> None:
    executed: list[str] = []

    class Cursor:
        def __enter__(self) -> "Cursor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, sql: str, params: object = None) -> None:
            executed.append(sql)

        def fetchone(self) -> dict[str, int]:
            return {"row_count": 10, "checksum": 99}

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    fingerprint = migration.protected_fingerprint(Connection(), "jw_mart_d2")

    assert fingerprint == migration.DbFingerprint(row_count=10, checksum=99)
    assert "audit_code_matrix" not in executed[0]
    for column in migration.PROTECTED_COLUMNS:
        assert column in executed[0]
