from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd

from pipeline.scripts.etl import build_f124a_general_dimension_staging as stage


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.rowcount = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.connection.executed.append((" ".join(sql.split()), params))
        if sql.lstrip().startswith("INSERT INTO"):
            self.rowcount = self.connection.insert_counts.pop(0) if self.connection.insert_counts else 0
        else:
            self.rowcount = 0

    def executemany(self, sql: str, params: list[tuple[object, ...]]) -> None:
        self.connection.executed.append((" ".join(sql.split()), params))
        self.rowcount = len(params)

    def fetchone(self) -> dict[str, object]:
        return self.connection.fetch_results.pop(0)


class FakeConnection:
    def __init__(
        self,
        *,
        insert_counts: list[int] | None = None,
        fetch_results: list[dict[str, object]] | None = None,
    ) -> None:
        self.executed: list[tuple[str, object]] = []
        self.insert_counts = list(insert_counts or [])
        self.fetch_results = list(fetch_results or [])

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


def test_guard_target_is_exact_live_db_and_f124a_staging_table() -> None:
    stage.guard_f124a_target(stage.F124A_TARGET_DB, stage.F124A_STAGING_TABLE)

    for db_name, table_name in (
        ("jw_mart", stage.F124A_STAGING_TABLE),
        (stage.F124A_TARGET_DB, stage.F124A_LIVE_TABLE),
        (stage.F124A_TARGET_DB, stage.F124A_STAGING_TABLE + "_other"),
    ):
        try:
            stage.guard_f124a_target(db_name, table_name)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe target accepted: {db_name}.{table_name}")


def test_verify_may_parquet_requires_expected_sha(tmp_path: Path) -> None:
    parquet = tmp_path / "year=2026" / "month=05" / "data.parquet"
    parquet.parent.mkdir(parents=True)
    parquet.write_bytes(b"verified-may")
    expected = hashlib.sha256(b"verified-may").hexdigest()

    assert stage.verify_may_parquet(tmp_path, expected) == parquet

    try:
        stage.verify_may_parquet(tmp_path, "0" * 64)
    except RuntimeError as exc:
        assert "sha256 mismatch" in str(exc)
    else:
        raise AssertionError("wrong parquet digest must fail")


def test_copy_live_table_preserves_rows_in_bounded_batches(monkeypatch) -> None:
    conn = FakeConnection(insert_counts=[2, 1, 0], fetch_results=[{"max_id": 2}, {"max_id": 3}])
    existing = {stage.F124A_LIVE_TABLE}
    monkeypatch.setattr(stage, "table_exists", lambda _conn, _db, table: table in existing)

    copied = stage.create_and_copy_live_table(conn, batch_size=2)

    assert copied == 3
    assert conn.executed[0][0].startswith("CREATE TABLE")
    inserts = [sql for sql, _params in conn.executed if sql.startswith("INSERT INTO")]
    assert inserts
    assert all("SELECT * FROM" in sql for sql in inserts)
    assert all("LIMIT 2" in sql for sql in inserts)


def test_merge_may_rows_only_patches_history_and_can_insert_new_keys() -> None:
    conn = FakeConnection()
    rows = [
        {
            "source": "ubist",
            "measure": "sales",
            "atc4_code": "A10N1",
            "brand_key": "brand",
            "brand_name": "Brand",
            "product_code": "product",
            "dimension_type": "seller",
            "dimension_value": "Seller",
            "dimension_value_norm": "seller",
            "raw_value_history": {"2026-05": 123.0},
        }
    ]

    merged = stage.merge_may_rows(conn, rows, batch_size=200)

    assert merged == 1
    sql, params = conn.executed[0]
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "JSON_MERGE_PATCH(raw_value_history, VALUES(raw_value_history))" in sql
    assert "2026-05" in str(params)


def test_merge_rejects_rows_with_nonmay_history() -> None:
    conn = FakeConnection()
    rows = [
        {
            "source": "ubist",
            "measure": "sales",
            "atc4_code": "A10N1",
            "brand_key": "brand",
            "brand_name": "Brand",
            "product_code": "product",
            "dimension_type": "seller",
            "dimension_value": "Seller",
            "dimension_value_norm": "seller",
            "raw_value_history": {"2026-04": 100.0, "2026-05": 123.0},
        }
    ]

    try:
        stage.merge_may_rows(conn, rows, batch_size=200)
    except RuntimeError as exc:
        assert "non-May period" in str(exc)
    else:
        raise AssertionError("non-May history must fail")

    assert conn.executed == []


def test_build_may_rows_streams_product_stable_partitions(monkeypatch, tmp_path: Path) -> None:
    frames = [
        pd.DataFrame([{"period_yyyymm": "2026-05", "partition": 1}]),
        pd.DataFrame([{"period_yyyymm": "2026-05", "partition": 2}]),
    ]
    merged: list[tuple[str, int]] = []

    monkeypatch.setattr(
        stage,
        "iter_ubist_base_frames",
        lambda **_kwargs: iter(frames),
    )
    monkeypatch.setattr(stage, "ubist_measure_frame", lambda frame, _measure: frame)
    monkeypatch.setattr(
        stage,
        "build_filter_dimension_rows",
        lambda _source, measure, frame: [
            {
                "measure": measure,
                "partition": int(frame.iloc[0]["partition"]),
            }
        ],
    )
    monkeypatch.setattr(
        stage,
        "merge_may_rows",
        lambda _conn, rows, **_kwargs: merged.append((rows[0]["measure"], rows[0]["partition"])) or len(rows),
    )

    measures, total = stage.build_and_merge_may_rows(
        FakeConnection(),
        spool_dir=tmp_path,
        target_db=stage.F124A_TARGET_DB,
        target_table=stage.F124A_STAGING_TABLE,
        batch_size=200,
    )

    assert measures == {"sales": 2, "volume": 2}
    assert total == 4
    assert merged == [("sales", 1), ("volume", 1), ("sales", 2), ("volume", 2)]
