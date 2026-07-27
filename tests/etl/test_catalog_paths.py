from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from pipeline.etl.io.catalog.paths import (
    CATALOG_MANIFEST_NAME,
    CATALOG_ROOT_ENV,
    CatalogEnvironmentError,
    CatalogIntegrityError,
    build_catalog_root,
    materialize_catalog,
    publish_catalog_outputs,
    resolve_catalog_root,
    validate_catalog_materialization,
)


@dataclass(frozen=True)
class _Result:
    name: str
    output_path: Path
    rows: int = 1
    columns: tuple[str, ...] = ("id",)


def _result(build_root: Path, name: str, payload: bytes = b"parquet") -> _Result:
    path = build_root / name / f"{name}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return _Result(name=name, output_path=path)


def test_publish_catalog_outputs_promotes_the_exact_build_artifacts(tmp_path: Path) -> None:
    output_root = tmp_path / "work"
    build_root = build_catalog_root(output_root)
    catalog_root = output_root / "output" / "catalog"
    results = [
        _result(build_root, "ml_market", b"ml"),
        _result(build_root, "cd_market", b"cd"),
        _result(build_root, "strategic_brand", b"brand"),
        _result(build_root, "strategic_product", b"product"),
    ]

    published = publish_catalog_outputs(
        results,
        build_root=build_root,
        catalog_root=catalog_root,
    )

    assert [item.name for item in published] == [item.name for item in results]
    for source, item in zip(results, published, strict=True):
        assert item.output_path == catalog_root / source.name / f"{source.name}.parquet"
        assert item.output_path.read_bytes() == source.output_path.read_bytes()
    assert (catalog_root / CATALOG_MANIFEST_NAME).is_file()
    validated = validate_catalog_materialization(
        catalog_root,
        required_names=frozenset(item.name for item in results),
    )
    assert [item.name for item in validated] == sorted(item.name for item in results)


def test_materialize_catalog_copies_a_checksumming_storage_snapshot(tmp_path: Path) -> None:
    build_root = tmp_path / "build"
    storage_root = tmp_path / "storage"
    destination = tmp_path / "runtime" / "catalog"
    results = [
        _result(build_root, "strategic_brand", b"brand"),
        _result(build_root, "strategic_product", b"product"),
    ]
    publish_catalog_outputs(results, build_root=build_root, catalog_root=storage_root)

    materialized = materialize_catalog(
        source_root=storage_root,
        destination_root=destination,
        required_names=frozenset({"strategic_brand", "strategic_product"}),
    )

    assert {item.name for item in materialized} == {"strategic_brand", "strategic_product"}
    assert (destination / "strategic_brand" / "strategic_brand.parquet").read_bytes() == b"brand"
    assert validate_catalog_materialization(
        destination,
        required_names=frozenset({"strategic_brand", "strategic_product"}),
    )


def test_catalog_manifest_is_byte_deterministic_for_the_same_artifacts(tmp_path: Path) -> None:
    manifests = []
    for suffix in ("first", "second"):
        build_root = tmp_path / f"build-{suffix}"
        catalog_root = tmp_path / f"catalog-{suffix}"
        publish_catalog_outputs(
            [
                _result(build_root, "strategic_brand", b"brand"),
                _result(build_root, "strategic_product", b"product"),
            ],
            build_root=build_root,
            catalog_root=catalog_root,
        )
        manifests.append((catalog_root / CATALOG_MANIFEST_NAME).read_bytes())

    assert manifests[0] == manifests[1]


def test_validate_catalog_distinguishes_missing_environment(tmp_path: Path) -> None:
    missing = tmp_path / "catalog"

    with pytest.raises(CatalogEnvironmentError, match="catalog root not found"):
        validate_catalog_materialization(
            missing,
            required_names=frozenset({"strategic_brand"}),
        )


def test_validate_catalog_rejects_corrupted_artifact(tmp_path: Path) -> None:
    build_root = tmp_path / "build"
    catalog_root = tmp_path / "catalog"
    publish_catalog_outputs(
        [_result(build_root, "strategic_brand", b"original")],
        build_root=build_root,
        catalog_root=catalog_root,
    )
    (catalog_root / "strategic_brand" / "strategic_brand.parquet").write_bytes(b"corrupt!")

    with pytest.raises(CatalogIntegrityError, match="SHA256 mismatch"):
        validate_catalog_materialization(
            catalog_root,
            required_names=frozenset({"strategic_brand"}),
        )


def test_validate_catalog_names_each_partially_missing_artifact(tmp_path: Path) -> None:
    build_root = tmp_path / "build"
    catalog_root = tmp_path / "catalog"
    publish_catalog_outputs(
        [
            _result(build_root, "strategic_brand", b"brand"),
            _result(build_root, "strategic_product", b"product"),
        ],
        build_root=build_root,
        catalog_root=catalog_root,
    )
    (catalog_root / "strategic_product" / "strategic_product.parquet").unlink()

    with pytest.raises(
        CatalogEnvironmentError,
        match=r"missing catalog artifact: strategic_product/strategic_product\.parquet",
    ):
        validate_catalog_materialization(
            catalog_root,
            required_names=frozenset({"strategic_brand", "strategic_product"}),
        )


def test_publish_catalog_outputs_rejects_a_missing_artifact_before_copying(tmp_path: Path) -> None:
    output_root = tmp_path / "work"
    build_root = build_catalog_root(output_root)
    catalog_root = output_root / "output" / "catalog"
    present = _result(build_root, "ml_market")
    missing = _Result(
        name="cd_market",
        output_path=build_root / "cd_market" / "cd_market.parquet",
    )

    with pytest.raises(FileNotFoundError, match="catalog build artifact not found"):
        publish_catalog_outputs(
            [present, missing],
            build_root=build_root,
            catalog_root=catalog_root,
        )

    assert not catalog_root.exists()


def test_publish_catalog_outputs_rejects_paths_outside_the_build_root(tmp_path: Path) -> None:
    output_root = tmp_path / "work"
    build_root = build_catalog_root(output_root)
    outside = _result(tmp_path / "other", "ml_market")

    with pytest.raises(ValueError, match="outside catalog build root"):
        publish_catalog_outputs(
            [outside],
            build_root=build_root,
            catalog_root=output_root / "output" / "catalog",
        )


def test_resolve_catalog_root_prefers_argument_then_env_then_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "work"
    env_root = tmp_path / "from-env"
    explicit_root = tmp_path / "explicit"
    monkeypatch.setenv(CATALOG_ROOT_ENV, str(env_root))

    assert resolve_catalog_root(output_root) == env_root
    assert resolve_catalog_root(output_root, explicit_root) == explicit_root
    monkeypatch.delenv(CATALOG_ROOT_ENV)
    assert resolve_catalog_root(output_root) == output_root / "output" / "catalog"


def test_s2_publishes_postfixed_catalog_before_db_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline.etl.stages import s2_catalog

    output_root = tmp_path / "work"
    build_root = build_catalog_root(output_root)
    catalog_root = output_root / "canonical"
    artifacts = [
        _result(build_root, "ml_market", b"ml"),
        _result(build_root, "cd_market", b"cd"),
        _result(build_root, "strategic_brand", b"brand"),
    ]
    monkeypatch.setattr(s2_catalog, "run_master_extracts", lambda **_: artifacts)
    monkeypatch.setattr(s2_catalog, "run_base_dimensions", lambda **_: [])
    monkeypatch.setattr(s2_catalog, "run_target_priority", lambda **_: [])
    monkeypatch.setattr(s2_catalog, "run_market_catalog", lambda **_: [])
    monkeypatch.setattr(s2_catalog, "run_brand_product_catalog", lambda **_: [])
    monkeypatch.setattr(s2_catalog, "run_postfix", lambda **_: [])
    monkeypatch.setattr(s2_catalog, "S2_REQUIRED_CATALOGS", frozenset(item.name for item in artifacts))
    calls: list[Path] = []

    def fake_sync(_conn, *, catalog_root: Path, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(catalog_root)
        assert (catalog_root / "ml_market" / "ml_market.parquet").read_bytes() == b"ml"
        assert (catalog_root / "cd_market" / "cd_market.parquet").read_bytes() == b"cd"
        assert (catalog_root / "strategic_brand" / "strategic_brand.parquet").read_bytes() == b"brand"
        return ()

    monkeypatch.setattr(s2_catalog, "sync_catalog_tables", fake_sync)

    rc = s2_catalog.run(
        {
            "target_dir": output_root,
            "catalog_root": catalog_root,
            "sync_catalog_db": True,
            "target_db": "jw_mart_rehearsal_test",
            "dry_run": True,
        }
    )

    assert rc == 0
    assert calls == [catalog_root]


def test_s2_does_not_sync_when_a_reported_build_artifact_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline.etl.stages import s2_catalog

    output_root = tmp_path / "work"
    missing = _Result(
        name="ml_market",
        output_path=build_catalog_root(output_root) / "ml_market" / "ml_market.parquet",
    )
    monkeypatch.setattr(s2_catalog, "run_master_extracts", lambda **_: [missing])
    monkeypatch.setattr(s2_catalog, "run_base_dimensions", lambda **_: [])
    monkeypatch.setattr(s2_catalog, "run_target_priority", lambda **_: [])
    monkeypatch.setattr(s2_catalog, "run_market_catalog", lambda **_: [])
    monkeypatch.setattr(s2_catalog, "run_brand_product_catalog", lambda **_: [])
    monkeypatch.setattr(s2_catalog, "run_postfix", lambda **_: [])
    monkeypatch.setattr(s2_catalog, "S2_REQUIRED_CATALOGS", frozenset({"ml_market"}))
    monkeypatch.setattr(
        s2_catalog,
        "sync_catalog_tables",
        lambda *_args, **_kwargs: pytest.fail("DB sync must not run after publish preflight fails"),
    )

    rc = s2_catalog.run(
        {
            "target_dir": output_root,
            "catalog_root": output_root / "canonical",
            "sync_catalog_db": True,
            "target_db": "jw_mart_rehearsal_test",
            "dry_run": True,
        }
    )

    assert rc == 1
    assert not (output_root / "canonical").exists()
