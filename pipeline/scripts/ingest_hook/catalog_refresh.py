"""Prepare a verified NFS catalog before an ingest mart build."""

from __future__ import annotations

import os
import re
import shutil
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from pipeline.etl.io.catalog.brand.brand_product_catalog import (
    run_cd_product,
    run_strategic_product,
)
from pipeline.etl.io.catalog.db_sync import (
    CatalogParityResult,
    compare_catalog_to_serving,
    export_serving_catalog_tables,
)
from pipeline.etl.io.catalog.paths import (
    S2_REQUIRED_CATALOGS,
    CatalogProvisioningError,
    build_catalog_root,
    catalog_file,
    publish_catalog_outputs,
    sha256_file,
    validate_catalog_materialization,
)
from pipeline.etl.io.catalog.postfix.molecule import apply_molecule_worklist
from pipeline.etl.io.catalog.postfix.rebuild_cd import rebuild_cd_brand
from pipeline.etl.stages import s2_catalog

MI_MASTER_FINGERPRINT = "mi_master_sha256"


@dataclass(frozen=True)
class CatalogPreparation:
    root: Path
    action: str
    mi_master_sha256: str
    parity: tuple[CatalogParityResult, ...]


@dataclass(frozen=True)
class _CatalogResult:
    name: str
    output_path: Path
    rows: int


def _mi_master_source_versions_match(
    mi_master: Path,
    source_file_versions: tuple[str, ...],
) -> bool:
    expected = unicodedata.normalize("NFC", mi_master.name)
    actual = tuple(
        unicodedata.normalize("NFC", Path(value).name)
        for value in source_file_versions
    )
    return actual == (expected,)


def ensure_nfs_catalog(
    *,
    catalog_root: Path,
    mi_master: Path,
    ubist_dir: Path,
    iqvia_nsa_dir: Path,
    target_db: str,
    conn,
    run_id: str,
    output_parent: Path,
    build: Callable[[Path, Path], int] | None = None,
    anchor: Callable[[Path, Path], None] | None = None,
) -> CatalogPreparation:
    """Reuse a matching snapshot or rebuild and atomically publish after DB parity."""

    root = Path(catalog_root).resolve()
    root_was_missing = not root.exists()
    master = Path(mi_master).resolve()
    master_sha = sha256_file(master)
    try:
        validate_catalog_materialization(
            root,
            required_names=S2_REQUIRED_CATALOGS,
            expected_source_fingerprints={MI_MASTER_FINGERPRINT: master_sha},
        )
    except CatalogProvisioningError:
        pass
    else:
        return CatalogPreparation(root, "reused", master_sha, ())

    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)
    candidate_output = Path(output_parent).resolve() / f".catalog-build-{safe_run_id}"
    candidate_root = candidate_output / "catalog"
    backup_root = root.with_name(f".{root.name}.backup-{safe_run_id}")
    if candidate_output.exists() or backup_root.exists():
        raise RuntimeError(
            "catalog refresh scratch already exists: "
            f"candidate={candidate_output} backup={backup_root}"
        )

    runner = build or (
        lambda output_root, destination: _run_s2_catalog(
            output_root,
            destination,
            mi_master=master,
            ubist_dir=Path(ubist_dir).resolve(),
            iqvia_nsa_dir=Path(iqvia_nsa_dir).resolve(),
        )
    )
    try:
        rc = runner(candidate_output, candidate_root)
        if rc != 0:
            raise RuntimeError(f"s2 catalog generation failed rc={rc}")
        validate_catalog_materialization(
            candidate_root,
            required_names=S2_REQUIRED_CATALOGS,
            expected_source_fingerprints={MI_MASTER_FINGERPRINT: master_sha},
        )
        parity = compare_catalog_to_serving(
            conn,
            target_db=target_db,
            catalog_root=candidate_root,
        )
        mismatches = tuple(result for result in parity if not result.matches)
        action = "rebuilt"
        if mismatches and root_was_missing:
            anchor_runner = anchor or (
                lambda output_root, destination: _anchor_candidate_to_serving(
                    output_root,
                    destination,
                    mi_master=master,
                    ubist_dir=Path(ubist_dir).resolve(),
                    iqvia_nsa_dir=Path(iqvia_nsa_dir).resolve(),
                    target_db=target_db,
                    conn=conn,
                )
            )
            try:
                anchor_runner(candidate_output, candidate_root)
            except Exception as exc:
                raise RuntimeError(f"catalog serving anchor rejected: {exc}") from exc
            validate_catalog_materialization(
                candidate_root,
                required_names=S2_REQUIRED_CATALOGS,
                expected_source_fingerprints={MI_MASTER_FINGERPRINT: master_sha},
            )
            parity = compare_catalog_to_serving(
                conn,
                target_db=target_db,
                catalog_root=candidate_root,
            )
            mismatches = tuple(result for result in parity if not result.matches)
            action = "serving-anchored"
        if mismatches:
            detail = "; ".join(
                f"{item.table_name}:candidate={item.candidate_rows},serving={item.serving_rows},"
                f"missing={len(item.missing_primary_keys)},added={len(item.added_primary_keys)},"
                f"changed={len(item.changed_primary_keys)}"
                for item in mismatches
            )
            raise RuntimeError(f"catalog serving parity mismatch: {detail}")

        if root.exists():
            os.replace(root, backup_root)
        try:
            root.parent.mkdir(parents=True, exist_ok=True)
            os.replace(candidate_root, root)
        except Exception:
            if backup_root.exists() and not root.exists():
                os.replace(backup_root, root)
            raise
        if backup_root.exists():
            shutil.rmtree(backup_root)
        return CatalogPreparation(root, action, master_sha, parity)
    finally:
        if candidate_output.exists():
            shutil.rmtree(candidate_output)


def _run_s2_catalog(
    output_root: Path,
    catalog_root: Path,
    *,
    mi_master: Path,
    ubist_dir: Path,
    iqvia_nsa_dir: Path,
) -> int:
    from pipeline.etl.lib.storage import PROJECT_ROOT

    return s2_catalog.run(
        {
            "target_dir": output_root,
            "input_file": mi_master,
            "catalog_root": catalog_root,
            "cache_dir": PROJECT_ROOT / "data" / "cache",
            "inputs_dir": PROJECT_ROOT / "inputs",
            "ubist_dir": ubist_dir,
            "iqvia_nsa_dir": iqvia_nsa_dir,
        }
    )


def _anchor_candidate_to_serving(
    output_root: Path,
    catalog_root: Path,
    *,
    mi_master: Path,
    ubist_dir: Path,
    iqvia_nsa_dir: Path,
    target_db: str,
    conn,
) -> None:
    from pipeline.etl.lib.storage import PROJECT_ROOT

    build_root = build_catalog_root(output_root)
    exports = export_serving_catalog_tables(
        conn,
        target_db=target_db,
        catalog_root=build_root,
    )
    expected_version = mi_master.name
    for item in exports:
        versions = tuple(Path(value).name for value in item.source_file_versions)
        if not _mi_master_source_versions_match(mi_master, item.source_file_versions):
            raise RuntimeError(
                f"{item.table_name} source version does not match MI Master: "
                f"expected={expected_version!r} actual={versions!r}"
            )

    run_strategic_product(
        output_root=output_root,
        input_file=mi_master,
        ubist_dir=ubist_dir,
        iqvia_nsa_dir=iqvia_nsa_dir,
    )
    rebuild_cd_brand(build_root)
    run_cd_product(output_root=output_root)
    apply_molecule_worklist(
        build_root,
        PROJECT_ROOT / "inputs" / "molecule_v4_worklist.csv",
    )

    results = tuple(
        _CatalogResult(
            name=name,
            output_path=catalog_file(build_root, name),
            rows=pq.ParquetFile(catalog_file(build_root, name)).metadata.num_rows,
        )
        for name in sorted(S2_REQUIRED_CATALOGS)
    )
    publish_catalog_outputs(
        results,
        build_root=build_root,
        catalog_root=catalog_root,
        required_names=S2_REQUIRED_CATALOGS,
        source_fingerprints={
            MI_MASTER_FINGERPRINT: sha256_file(mi_master),
            **{
                f"serving_{item.parquet_name}_sha256": item.manifest_hash
                for item in exports
            },
        },
    )
