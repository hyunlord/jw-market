from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from pipeline.etl.io.catalog.paths import publish_catalog_outputs
from pipeline.scripts.deploy import strategic_reload_publish as publish
from pipeline.scripts.deploy.mart_load_ops import PublishAction
from pipeline.scripts.deploy.mart_load_verify import CanonicalDigest


@dataclass(frozen=True)
class _CatalogResult:
    name: str
    output_path: Path
    rows: int = 1


@pytest.fixture
def provisioned_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project_root = tmp_path / "repo"
    catalog_root = project_root / "output" / "catalog"
    build_root = tmp_path / "catalog-build"
    results = []
    for name in sorted(publish.PUBLISH_REQUIRED_CATALOGS):
        output_path = build_root / name / f"{name}.parquet"
        output_path.parent.mkdir(parents=True)
        output_path.write_bytes(f"fixture:{name}".encode())
        results.append(_CatalogResult(name=name, output_path=output_path))
    publish_catalog_outputs(
        results,
        build_root=build_root,
        catalog_root=catalog_root,
    )
    monkeypatch.setattr(publish, "PROJECT_ROOT", project_root)
    return catalog_root


def test_strategic_reload_tables_are_exact_eight_body_tables() -> None:
    assert publish.STRATEGIC_RELOAD_TABLES == (
        "mart_strategic_ml_brand_metric",
        "mart_strategic_cd_brand_metric",
        "mart_strategic_ml_market_metric",
        "mart_strategic_cd_market_metric",
        "cache_brands",
        "cache_market_status",
        "cache_cause",
        "cache_deep_analysis",
    )


def test_legacy_publish_allowlist_rejects_analysis_level_blocks() -> None:
    try:
        publish.validate_publish_tables(("mart_analysis_level_block",))
    except ValueError as exc:
        assert "mart_analysis_level_block" in str(exc)
    else:
        raise AssertionError("MALB must use the paired blue-green publisher")


def test_validate_publish_tables_rejects_general_mart() -> None:
    try:
        publish.validate_publish_tables(("mart_strategic_ml_brand_metric", "mart_general_brand_metric"))
    except ValueError as exc:
        assert "mart_general_brand_metric" in str(exc)
    else:
        raise AssertionError("expected general mart table to be rejected")


def test_guard_publish_requires_explicit_operating_target_flag() -> None:
    try:
        publish.guard_publish_run(build_db="jw_mart_pubtest_build", target_db="jw_mart", allow_operating_target=False)
    except RuntimeError as exc:
        assert "--allow-operating-target" in str(exc)
    else:
        raise AssertionError("expected protected target guard")


def test_resolve_catalog_root_requires_output_catalog(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "repo"
    output_catalog = project_root / "output" / "catalog"
    parquet = project_root / "parquet"
    build_root = tmp_path / "catalog-build"
    results = []
    for name in sorted(publish.PUBLISH_REQUIRED_CATALOGS):
        output_path = build_root / name / f"{name}.parquet"
        output_path.parent.mkdir(parents=True)
        output_path.write_bytes(name.encode())
        results.append(_CatalogResult(name=name, output_path=output_path))
    publish_catalog_outputs(
        results,
        build_root=build_root,
        catalog_root=output_catalog,
    )
    parquet.mkdir()
    monkeypatch.setattr(publish, "PROJECT_ROOT", project_root)

    assert publish.resolve_publish_catalog_root(output_catalog) == output_catalog.resolve()

    try:
        publish.resolve_publish_catalog_root(parquet)
    except ValueError as exc:
        assert "output/catalog" in str(exc)
    else:
        raise AssertionError("expected non-output catalog root to be rejected")


def test_publish_calls_atomic_rename_for_each_reload_table(
    monkeypatch, provisioned_catalog: Path
) -> None:
    calls: list[tuple[str, str, str, str]] = []

    def fake_publish_one(conn: object, build_db: str, target_db: str, table_name: str, run_id: str) -> PublishAction:
        calls.append((build_db, target_db, table_name, run_id))
        return PublishAction(table_name, "atomic_rename", table_name, f"{table_name}__old_{run_id}", 3)

    monkeypatch.setattr(publish, "_publish_one", fake_publish_one)

    summary = publish.publish_strategic_reload_tables(
        object(),
        build_db="jw_mart_pubtest_build",
        target_db="jw_mart_pubtest_target",
        run_id="run123",
    )

    assert [call[2] for call in calls] == list(publish.STRATEGIC_RELOAD_TABLES)
    assert summary.rolled_back is False
    assert len(summary.actions) == 8


def test_dry_run_checks_rows_without_swapping(
    monkeypatch, provisioned_catalog: Path
) -> None:
    published: list[str] = []

    def fake_publish_one(*args: object, **kwargs: object) -> PublishAction:
        published.append("called")
        return PublishAction("unexpected", "atomic_rename", "unexpected", None, 0)

    def fake_table_digest(conn: object, db_name: str, table_name: str) -> CanonicalDigest:
        return CanonicalDigest(row_count=len(table_name), sha256=f"sha-{table_name}")

    monkeypatch.setattr(publish, "_publish_one", fake_publish_one)
    monkeypatch.setattr(publish, "table_digest", fake_table_digest)

    summary = publish.publish_strategic_reload_tables(
        object(),
        build_db="jw_mart_pubtest_build",
        target_db="jw_mart_pubtest_target",
        run_id="run123",
        dry_run=True,
    )

    assert published == []
    assert summary.dry_run is True
    assert summary.actions[0].mode == "dry_run"
    assert summary.actions[0].row_count == len("mart_strategic_ml_brand_metric")


def test_publish_restores_successful_backups_after_later_failure(
    monkeypatch, provisioned_catalog: Path
) -> None:
    published: list[str] = []
    restored: list[str] = []

    def fake_publish_one(conn: object, build_db: str, target_db: str, table_name: str, run_id: str) -> PublishAction:
        published.append(table_name)
        if table_name == "mart_strategic_cd_brand_metric":
            raise RuntimeError("boom")
        return PublishAction(table_name, "atomic_rename", table_name, f"{table_name}__old_{run_id}", 3)

    def fake_restore(conn: object, target_db: str, action: PublishAction, run_id: str) -> None:
        restored.append(action.table)

    monkeypatch.setattr(publish, "_publish_one", fake_publish_one)
    monkeypatch.setattr(publish, "restore_published_table", fake_restore)

    try:
        publish.publish_strategic_reload_tables(
            object(),
            build_db="jw_mart_pubtest_build",
            target_db="jw_mart_pubtest_target",
            run_id="run123",
        )
    except publish.PublishFailedError as exc:
        assert "boom" in str(exc.__cause__)
    else:
        raise AssertionError("expected publish failure")

    assert published == ["mart_strategic_ml_brand_metric", "mart_strategic_cd_brand_metric"]
    assert restored == ["mart_strategic_ml_brand_metric"]
