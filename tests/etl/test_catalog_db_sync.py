from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from pipeline.etl.io.catalog import db_sync


@dataclass
class Cursor:
    statements: list[str]
    batches: list[int]

    def __enter__(self) -> "Cursor":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self.statements.append(sql)

    def executemany(self, sql: str, values: list[tuple[object, ...]]) -> None:
        self.statements.append(sql)
        self.batches.append(len(values))


@dataclass
class Connection:
    statements: list[str] = field(default_factory=list)
    batches: list[int] = field(default_factory=list)
    commits: int = 0

    def cursor(self) -> Cursor:
        return Cursor(self.statements, self.batches)

    def commit(self) -> None:
        self.commits += 1


def test_sync_catalog_tables_upserts_output_catalog_with_batch_cap(tmp_path: Path) -> None:
    # Given: finalized output/catalog parquet files, including a >200 row brand catalog.
    _write_parquet(tmp_path, "ml_market", [_ml_row("ml_006")])
    _write_parquet(tmp_path, "cd_market", [_cd_row("cd_006")])
    _write_parquet(tmp_path, "strategic_brand", [_brand_row(index) for index in range(205)])
    conn = Connection()

    # When: syncing catalog tables with an oversized requested batch.
    results = db_sync.sync_catalog_tables(
        conn,
        target_db="jw_mart_d2_stage_20260630_r2",
        catalog_root=tmp_path,
        batch_size=10000,
    )

    # Then: only catalog tables are created/upserted and write batches are capped at 200.
    assert [result.table_name for result in results] == [
        "catalog_ml_market",
        "catalog_cd_market",
        "catalog_strategic_brand",
    ]
    assert [result.rows for result in results] == [1, 1, 205]
    assert all(result.batch_size == 200 for result in results)
    assert conn.batches == [1, 1, 200, 5]
    assert all("catalog_" in statement for statement in conn.statements)
    assert any("CREATE TABLE IF NOT EXISTS `jw_mart_d2_stage_20260630_r2`.`catalog_ml_market`" in s for s in conn.statements)
    assert any("ON DUPLICATE KEY UPDATE" in s for s in conn.statements)


def test_sync_catalog_tables_dry_run_does_not_write(tmp_path: Path) -> None:
    # Given: the three required finalized catalog parquet files.
    _write_parquet(tmp_path, "ml_market", [_ml_row("ml_006")])
    _write_parquet(tmp_path, "cd_market", [_cd_row("cd_006")])
    _write_parquet(tmp_path, "strategic_brand", [_brand_row(1)])
    conn = Connection()

    # When: dry-running the sync.
    results = db_sync.sync_catalog_tables(conn, target_db="scratch", catalog_root=tmp_path, dry_run=True)

    # Then: parquet is inspected but no DDL/UPSERT is executed.
    assert [result.rows for result in results] == [1, 1, 1]
    assert conn.statements == []
    assert conn.commits == 0


def test_sync_catalog_tables_requires_output_catalog_layout(tmp_path: Path) -> None:
    # Given: a root parquet-style location instead of output/catalog/<name>/<name>.parquet.
    wrong_layout = tmp_path / "strategic_brand"
    wrong_layout.mkdir()

    # When / Then: the sync refuses to infer or fall back to root parquet files.
    try:
        db_sync.sync_catalog_tables(Connection(), target_db="scratch", catalog_root=tmp_path)
    except FileNotFoundError as exc:
        assert "ml_market/ml_market.parquet" in str(exc)
    else:
        raise AssertionError("expected missing output/catalog layout to fail")


def test_catalog_parity_uses_full_pk_order_and_reports_changed_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate_by_name = {
        "ml_market": [{"ml_id": "ml_1", "name": "candidate"}],
        "cd_market": [{"cd_id": "cd_1", "name": "same"}],
        "strategic_brand": [{"brand_id": "brand_1", "name": "same"}],
    }

    def fake_load(_root: Path, spec: db_sync.CatalogTableSpec):
        return candidate_by_name[spec.parquet_name], tmp_path / "unused", "sha"

    monkeypatch.setattr(db_sync, "_load_catalog_rows", fake_load)
    queries: list[str] = []

    class ParityCursor:
        def __init__(self):
            self.sql = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql: str) -> None:
            self.sql = sql
            queries.append(sql)

        def fetchall(self):
            if "catalog_ml_market" in self.sql:
                return [{"ml_id": "ml_1", "name": "serving"}]
            if "catalog_cd_market" in self.sql:
                return [{"cd_id": "cd_1", "name": "same"}]
            return [{"brand_id": "brand_1", "name": "same"}]

    class ParityConnection:
        def cursor(self):
            return ParityCursor()

    results = db_sync.compare_catalog_to_serving(
        ParityConnection(),
        target_db="serving",
        catalog_root=tmp_path,
    )

    assert results[0].changed_primary_keys == ("ml_1",)
    assert results[1].matches
    assert results[2].matches
    assert all("ORDER BY" in query for query in queries)


def test_replacement_sync_rejects_unapproved_removal_before_dml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: serving has an ID absent from the candidate catalog.
    _write_parquet(tmp_path, "ml_market", [_ml_row("ml_keep")])
    _write_parquet(tmp_path, "cd_market", [_cd_row("cd_keep")])
    _write_parquet(tmp_path, "strategic_brand", [_brand_row(1)])
    conn = ReplacementConnection(
        current_ids={"catalog_ml_market": ("ml_keep", "ml_remove")}
    )

    # When / Then: replacement sync fails before DELETE/UPSERT/COMMIT.
    with pytest.raises(ValueError, match="removed catalog IDs require exact approval"):
        db_sync.sync_catalog_tables(
            conn,
            target_db="scratch",
            catalog_root=tmp_path,
            replacement=db_sync.CatalogReplacementApproval(removed_ids_by_table={}),
            reference_report=db_sync.CatalogReplacementReferenceReport(),
        )

    assert not any(statement.startswith("DELETE FROM") for statement in conn.statements)
    assert conn.batches == []
    assert conn.commits == 0
    assert conn.rollbacks == 0


def test_replacement_sync_rolls_back_when_post_write_parity_mismatches(
    tmp_path: Path,
) -> None:
    # Given: replacement has exact approval but serving parity stays mismatched.
    _write_parquet(tmp_path, "ml_market", [_ml_row("ml_keep")])
    _write_parquet(tmp_path, "cd_market", [_cd_row("cd_keep")])
    _write_parquet(tmp_path, "strategic_brand", [_brand_row(1)])
    conn = ReplacementConnection(
        current_ids={"catalog_ml_market": ("ml_keep", "ml_remove")},
        parity_rows={
            "catalog_ml_market": [_ml_row("ml_keep"), _ml_row("ml_remove")],
            "catalog_cd_market": [_cd_row("cd_keep")],
            "catalog_strategic_brand": [_brand_row(1)],
        },
    )

    # When / Then: the transaction is rolled back after parity rejects the write.
    with pytest.raises(RuntimeError, match="catalog replacement parity failed"):
        db_sync.sync_catalog_tables(
            conn,
            target_db="scratch",
            catalog_root=tmp_path,
            replacement=db_sync.CatalogReplacementApproval(
                removed_ids_by_table={"catalog_ml_market": ("ml_remove",)}
            ),
            reference_report=db_sync.CatalogReplacementReferenceReport(grounded=True),
        )

    assert any(statement.startswith("DELETE FROM") for statement in conn.statements)
    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_replacement_sync_rejects_referenced_removal_without_inactive_decision(
    tmp_path: Path,
) -> None:
    # Given: removed ID approval exists but the ID is still referenced.
    _write_parquet(tmp_path, "ml_market", [_ml_row("ml_keep")])
    _write_parquet(tmp_path, "cd_market", [_cd_row("cd_keep")])
    _write_parquet(tmp_path, "strategic_brand", [_brand_row(1)])
    conn = ReplacementConnection(
        current_ids={"catalog_ml_market": ("ml_keep", "ml_remove")}
    )

    # When / Then: referenced removals still require an inactive decision before DML.
    with pytest.raises(ValueError, match="referenced catalog removals"):
        db_sync.sync_catalog_tables(
            conn,
            target_db="scratch",
            catalog_root=tmp_path,
            replacement=db_sync.CatalogReplacementApproval(
                removed_ids_by_table={"catalog_ml_market": ("ml_remove",)}
            ),
            reference_report=db_sync.CatalogReplacementReferenceReport(
                referenced_ids_by_table={"catalog_ml_market": ("ml_remove",)},
                grounded=True,
            ),
        )

    assert not any(statement.startswith("DELETE FROM") for statement in conn.statements)
    assert conn.batches == []
    assert conn.commits == 0


def test_replacement_sync_rejects_ungrounded_removal_reference_report(
    tmp_path: Path,
) -> None:
    # Given: removed ID approval exists but the reference report was not DB-grounded.
    _write_parquet(tmp_path, "ml_market", [_ml_row("ml_keep")])
    _write_parquet(tmp_path, "cd_market", [_cd_row("cd_keep")])
    _write_parquet(tmp_path, "strategic_brand", [_brand_row(1)])
    conn = ReplacementConnection(
        current_ids={"catalog_ml_market": ("ml_keep", "ml_remove")}
    )

    # When / Then: hand-authored reference data cannot authorize removals.
    with pytest.raises(ValueError, match="DB-grounded reference report"):
        db_sync.sync_catalog_tables(
            conn,
            target_db="scratch",
            catalog_root=tmp_path,
            replacement=db_sync.CatalogReplacementApproval(
                removed_ids_by_table={"catalog_ml_market": ("ml_remove",)}
            ),
            reference_report=db_sync.CatalogReplacementReferenceReport(
                referenced_ids_by_table={},
                inactive_decisions_by_table={},
            ),
        )

    assert not any(statement.startswith("DELETE FROM") for statement in conn.statements)
    assert conn.batches == []
    assert conn.commits == 0


def test_replacement_sync_deletes_approved_ids_and_passes_parity(tmp_path: Path) -> None:
    # Given: replacement approval exactly names removed IDs and references are inactive.
    ml_candidate = _ml_row("ml_keep")
    cd_candidate = _cd_row("cd_keep")
    brand_candidate = _brand_row(1)
    _write_parquet(tmp_path, "ml_market", [ml_candidate])
    _write_parquet(tmp_path, "cd_market", [cd_candidate])
    _write_parquet(tmp_path, "strategic_brand", [brand_candidate])
    conn = ReplacementConnection(
        current_ids={
            "catalog_ml_market": ("ml_keep", "ml_remove"),
            "catalog_cd_market": ("cd_keep",),
            "catalog_strategic_brand": ("brand_0001",),
        },
        parity_rows={
            "catalog_ml_market": [ml_candidate],
            "catalog_cd_market": [cd_candidate],
            "catalog_strategic_brand": [brand_candidate],
        },
    )

    # When: an explicit replacement sync is approved.
    results = db_sync.sync_catalog_tables(
        conn,
        target_db="scratch",
        catalog_root=tmp_path,
        replacement=db_sync.CatalogReplacementApproval(
            removed_ids_by_table={"catalog_ml_market": ("ml_remove",)}
        ),
        reference_report=db_sync.CatalogReplacementReferenceReport(
            referenced_ids_by_table={"catalog_ml_market": ("ml_remove",)},
            inactive_decisions_by_table={"catalog_ml_market": ("ml_remove",)},
            grounded=True,
        ),
    )

    # Then: only the approved missing ID is deleted and the replacement commits once.
    delete_statements = [
        statement for statement in conn.statements if statement.startswith("DELETE FROM")
    ]
    assert delete_statements == [
        "DELETE FROM `scratch`.`catalog_ml_market` WHERE `ml_id` IN (%s)"
    ]
    assert conn.delete_values == [("ml_remove",)]
    assert [result.table_name for result in results] == [
        "catalog_ml_market",
        "catalog_cd_market",
        "catalog_strategic_brand",
    ]
    assert conn.commits == 1
    assert conn.rollbacks == 0


@dataclass
class ReplacementCursor:
    conn: "ReplacementConnection"
    sql: str = ""

    def __enter__(self) -> "ReplacementCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, values: tuple[object, ...] | None = None) -> None:
        self.sql = sql
        self.conn.statements.append(sql)
        if sql.startswith("DELETE FROM"):
            self.conn.delete_values.append(tuple(values or ()))

    def executemany(self, sql: str, values: list[tuple[object, ...]]) -> None:
        self.conn.statements.append(sql)
        self.conn.batches.append(len(values))

    def fetchall(self) -> list[dict[str, object]]:
        table = self._table_name()
        if self._selects_only_primary_key():
            return [
                self._pk_row(value)
                for value in self.conn.current_ids.get(table, ())
            ]
        return list(self.conn.parity_rows.get(table, ()))

    def _selects_only_primary_key(self) -> bool:
        table = self._table_name()
        primary_key = {
            "catalog_ml_market": "ml_id",
            "catalog_cd_market": "cd_id",
            "catalog_strategic_brand": "brand_id",
        }[table]
        return self.sql.startswith(f"SELECT `{primary_key}` FROM")

    def _table_name(self) -> str:
        for table in ("catalog_ml_market", "catalog_cd_market", "catalog_strategic_brand"):
            if f"`{table}`" in self.sql:
                return table
        raise AssertionError(f"unknown table in SQL: {self.sql}")

    def _pk_row(self, value: str) -> dict[str, object]:
        table = self._table_name()
        if table == "catalog_ml_market":
            return {"ml_id": value}
        if table == "catalog_cd_market":
            return {"cd_id": value}
        return {"brand_id": value}


@dataclass
class ReplacementConnection:
    current_ids: dict[str, tuple[str, ...]]
    parity_rows: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    statements: list[str] = field(default_factory=list)
    batches: list[int] = field(default_factory=list)
    delete_values: list[tuple[object, ...]] = field(default_factory=list)
    commits: int = 0
    rollbacks: int = 0

    def cursor(self) -> ReplacementCursor:
        return ReplacementCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _write_parquet(root: Path, name: str, rows: list[dict[str, object]]) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, directory / f"{name}.parquet")


def _ml_row(ml_id: str) -> dict[str, object]:
    return {
        "ml_id": ml_id,
        "name": "리바로 리바로젯",
        "data_source": "UBIST",
        "atc_codes_json": "[\"C10A1\", \"C10C\"]",
        "analyze_class": True,
        "analyze_molecule": True,
        "analyze_dosage_form": False,
        "analyze_strength_pack": True,
        "analyze_nhi_type": False,
        "analyze_ox_gx": True,
        "analyze_fish_oil": False,
        "target_iqvia_1": None,
        "target_iqvia_2": None,
        "target_iqvia_3": None,
        "target_ubist_1": "순환기",
        "target_ubist_2": "내분비",
        "target_ubist_3": None,
        "target_ubist_4": None,
        "source_file_version": "MI Master 2026.05.18.xlsx",
        "ingested_at": datetime(2026, 5, 18, 0, 0, 0),
    }


def _cd_row(cd_id: str) -> dict[str, object]:
    row = _ml_row("ml_006")
    row.update({"cd_id": cd_id, "cd_filter_id": "cf_006"})
    return row


def _brand_row(index: int) -> dict[str, object]:
    return {
        "brand_id": f"brand_{index:04d}",
        "name": f"브랜드{index}",
        "merge_name": f"브랜드{index}",
        "ml_id": "ml_006",
        "cd_id": "cd_006",
        "is_excluded": False,
        "is_class_excluded": False,
        "allowed_atc4_codes_json": "[\"C10A1\"]",
        "class": "C10",
        "class_1": "C",
        "class_2": "C10A",
        "molecule": "pitavastatin",
        "dosage_form": "tablet",
        "strength_pack": "2mg",
        "nhi_type": "급여",
        "ox_gx": "O",
        "fish_oil": None,
        "판매사": "JW중외제약",
        "제조사": "JW중외제약",
        "source_file_version": "MI Master 2026.05.18.xlsx",
        "ingested_at": datetime(2026, 5, 18, 0, 0, 0),
        "is_jw": index == 0,
        "is_target": index == 0,
        "canonical_name": "리바로" if index == 0 else None,
        "general_brand_key": "리바로" if index == 0 else None,
        "strategy_id": "strategy_006",
    }
