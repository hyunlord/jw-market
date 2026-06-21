from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pipeline.etl.io.mart import molecule_bridge_build
from pipeline.etl.io.mart.molecule_bridge_schema import BRIDGE_INSERT_COLUMNS
from pipeline.scripts.deploy import mart_load_ops
from pipeline.scripts.deploy.mart_load_verify import _canonical_value, _stable_column_expression


def test_build_strategic_ml_market_rows_tolerates_missing_channel_specialty_matrix() -> None:
    rows = mart_load_ops.build_strategic_ml_market_rows(
        [
            {
                "ml_id": "ml_001",
                "brand_id": "b1",
                "brand_key": "brand-a",
                "brand_name": "Brand A",
                "source": "ubist",
                "measure": "sales",
                "unit_label": "KRW",
                "metric_history": {"2025": {"raw_value": 100.0, "ms": 100.0}},
                "extended_metric_history": {"2025": {"ei_5y": 1.0, "momentum_score": 2.0}},
                "channel_data": {},
                "specialty_data": {},
                "dimension_data": {},
                "dimension_channel_data": {},
                "dimension_specialty_data": {},
                "by_dimension": {"company": "Acme"},
                "raw_value_history": {"2025": 100.0},
                "overlay_data": {},
                "payload": {},
            }
        ],
        {"ml_001": {"ml_id": "ml_001", "name": "Market One"}},
    )

    assert len(rows) == 1
    assert rows[0]["ml_id"] == "ml_001"
    assert rows[0]["market_size_series"] == {"2025": 100.0}
    assert rows[0]["target_customer_competition"]["source_type"] == "computed"


def test_payload_checksum_expression_ignores_only_computed_at() -> None:
    expression = _stable_column_expression("payload")

    assert "JSON_REMOVE" in expression
    assert "$.computed_at" in expression
    assert "payload" in expression


def test_canonical_value_rounds_float_serialization_noise() -> None:
    left = _canonical_value('{"2021-01": 77684602.7, "nested": [1.0000000001]}')
    right = _canonical_value('{"nested": [1.0], "2021-01": 77684602.69999999}')

    assert left == right


def test_run_bridge_reads_from_source_db_and_writes_build_db(monkeypatch) -> None:
    calls: list[dict[str, str | Path]] = []

    def fake_build_molecule_bridge(*, source_db: str, target_db: str, catalog_root: Path) -> SimpleNamespace:
        calls.append({"source_db": source_db, "target_db": target_db, "catalog_root": catalog_root})
        return SimpleNamespace(
            source_db=source_db,
            target_db=target_db,
            inserted_rows=58_330,
            candidate_rows=60_000,
            brand_keys=10,
            molecule_norms=20,
            combo_rows=30,
        )

    monkeypatch.setattr(mart_load_ops, "build_molecule_bridge", fake_build_molecule_bridge)
    monkeypatch.setattr(mart_load_ops, "first_existing", lambda *paths: Path("/tmp/catalog"))

    mart_load_ops.run_bridge(build_db="scratch_build", source_db="jw_mart", catalog_root=None)

    assert calls == [{"source_db": "jw_mart", "target_db": "scratch_build", "catalog_root": Path("/tmp/catalog")}]


def test_bridge_insert_payloads_are_batched() -> None:
    batches: list[int] = []

    class Cursor:
        def __enter__(self) -> "Cursor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def executemany(self, sql: str, values: list[tuple[object, ...]]) -> None:
            assert "mart_brand_molecule" in sql
            batches.append(len(values))

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    payloads = [
        {column: (idx if column.endswith("_count") or column == "is_combo_component" else f"{column}-{idx}") for column in BRIDGE_INSERT_COLUMNS}
        for idx in range(5)
    ]

    molecule_bridge_build._insert_payloads(Connection(), "scratch_build", payloads, batch_size=2)

    assert batches == [2, 2, 1]


def test_copy_table_batches_by_id(monkeypatch) -> None:
    executed: list[tuple[str, tuple[int, int] | None]] = []

    class Cursor:
        def __enter__(self) -> "Cursor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, sql: str, params: tuple[int, int] | None = None) -> None:
            executed.append((sql, params))

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    monkeypatch.setattr(mart_load_ops, "_ordered_columns", lambda *args: ["id", "payload"])
    monkeypatch.setattr(mart_load_ops, "_id_bounds", lambda *args: (1, 5))

    mart_load_ops._copy_table(Connection(), "build_db", "target_db", "source_table", "target_table", batch_size=2)

    assert "CREATE TABLE" in executed[0][0]
    assert [params for _, params in executed[1:]] == [(1, 2), (3, 4), (5, 6)]
    assert all("WHERE id BETWEEN %s AND %s" in sql for sql, _ in executed[1:])
