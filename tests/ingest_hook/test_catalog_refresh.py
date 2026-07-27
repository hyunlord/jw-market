from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from pipeline.etl.io.catalog.db_sync import CatalogParityResult
from pipeline.etl.io.catalog.paths import (
    S2_REQUIRED_CATALOGS,
    publish_catalog_outputs,
    sha256_file,
)
from pipeline.scripts.ingest_hook import catalog_refresh


@dataclass(frozen=True)
class _Result:
    name: str
    output_path: Path
    rows: int = 1


def _publish(root: Path, mi_master: Path, payload: bytes) -> None:
    build_root = root.parent / f"{root.name}-build"
    results = []
    for name in sorted(S2_REQUIRED_CATALOGS):
        path = build_root / name / f"{name}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table({"payload": [payload + name.encode()]}), path)
        results.append(_Result(name, path))
    publish_catalog_outputs(
        results,
        build_root=build_root,
        catalog_root=root,
        required_names=S2_REQUIRED_CATALOGS,
        source_fingerprints={
            catalog_refresh.MI_MASTER_FINGERPRINT: sha256_file(mi_master)
        },
    )


def _matching_parity() -> tuple[CatalogParityResult, ...]:
    return (
        CatalogParityResult("ml_market", "catalog_ml_market", 16, 16, (), (), ()),
        CatalogParityResult("cd_market", "catalog_cd_market", 19, 19, (), (), ()),
        CatalogParityResult(
            "strategic_brand", "catalog_strategic_brand", 5100, 5100, (), (), ()
        ),
    )


def _ensure_args(tmp_path: Path, catalog_root: Path, mi_master: Path) -> dict:
    return {
        "catalog_root": catalog_root,
        "mi_master": mi_master,
        "ubist_dir": tmp_path / "ubist",
        "iqvia_nsa_dir": tmp_path / "iqvia",
        "target_db": "serving",
        "conn": object(),
        "run_id": "run-1",
        "output_parent": tmp_path / "runtime",
    }


def test_matching_mi_master_reuses_complete_nfs_catalog(tmp_path: Path) -> None:
    master = tmp_path / "mi.xlsx"
    master.write_bytes(b"same")
    root = tmp_path / "catalog"
    _publish(root, master, b"existing")

    result = catalog_refresh.ensure_nfs_catalog(
        **_ensure_args(tmp_path, root, master),
        build=lambda *_: pytest.fail("matching snapshot must not rebuild"),
    )

    assert result.action == "reused"
    assert result.parity == ()


def test_changed_mi_master_rebuilds_only_after_serving_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_master = tmp_path / "old.xlsx"
    old_master.write_bytes(b"old")
    master = tmp_path / "mi.xlsx"
    master.write_bytes(b"new")
    root = tmp_path / "catalog"
    _publish(root, old_master, b"old")
    monkeypatch.setattr(
        catalog_refresh,
        "compare_catalog_to_serving",
        lambda *_args, **_kwargs: _matching_parity(),
    )

    def build(_output: Path, candidate: Path) -> int:
        _publish(candidate, master, b"new")
        return 0

    result = catalog_refresh.ensure_nfs_catalog(
        **_ensure_args(tmp_path, root, master),
        build=build,
    )

    assert result.action == "rebuilt"
    assert result.parity == _matching_parity()
    assert (
        pq.read_table(root / "strategic_brand" / "strategic_brand.parquet")
        .column("payload")
        .to_pylist()[0]
        .startswith(b"new")
    )


def test_catalog_parity_mismatch_keeps_existing_nfs_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_master = tmp_path / "old.xlsx"
    old_master.write_bytes(b"old")
    master = tmp_path / "mi.xlsx"
    master.write_bytes(b"new")
    root = tmp_path / "catalog"
    _publish(root, old_master, b"old")
    before = (root / "strategic_brand" / "strategic_brand.parquet").read_bytes()
    monkeypatch.setattr(
        catalog_refresh,
        "compare_catalog_to_serving",
        lambda *_args, **_kwargs: (
            CatalogParityResult(
                "strategic_brand",
                "catalog_strategic_brand",
                3874,
                5100,
                ("serving-only",),
                (),
                (),
            ),
        ),
    )

    def build(_output: Path, candidate: Path) -> int:
        _publish(candidate, master, b"candidate")
        return 0

    with pytest.raises(RuntimeError, match="serving parity mismatch"):
        catalog_refresh.ensure_nfs_catalog(
            **_ensure_args(tmp_path, root, master),
            build=build,
        )

    assert (root / "strategic_brand" / "strategic_brand.parquet").read_bytes() == before


def test_missing_mi_master_fails_before_catalog_generation(tmp_path: Path) -> None:
    missing = tmp_path / "missing.xlsx"

    with pytest.raises(FileNotFoundError, match="catalog source file not found"):
        catalog_refresh.ensure_nfs_catalog(
            **_ensure_args(tmp_path, tmp_path / "catalog", missing),
            build=lambda *_: pytest.fail("missing input must not build"),
        )


def test_ingest_catalog_rebuild_uses_repository_molecule_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline.etl.lib.storage import PROJECT_ROOT

    captured: dict[str, Path] = {}

    def fake_run(params: dict[str, Path]) -> int:
        captured.update(params)
        return 0

    monkeypatch.setattr(catalog_refresh.s2_catalog, "run", fake_run)

    rc = catalog_refresh._run_s2_catalog(
        tmp_path / "output",
        tmp_path / "catalog",
        mi_master=tmp_path / "mi.xlsx",
        ubist_dir=tmp_path / "ubist",
        iqvia_nsa_dir=tmp_path / "iqvia",
    )

    assert rc == 0
    assert captured["inputs_dir"] == PROJECT_ROOT / "inputs"
