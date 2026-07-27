"""Prepare a verified NFS catalog before an ingest mart build."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import shutil
from typing import Callable

from pipeline.etl.io.catalog.db_sync import (
    CatalogParityResult,
    compare_catalog_to_serving,
)
from pipeline.etl.io.catalog.paths import (
    S2_REQUIRED_CATALOGS,
    CatalogProvisioningError,
    sha256_file,
    validate_catalog_materialization,
)
from pipeline.etl.stages import s2_catalog


MI_MASTER_FINGERPRINT = "mi_master_sha256"


@dataclass(frozen=True)
class CatalogPreparation:
    root: Path
    action: str
    mi_master_sha256: str
    parity: tuple[CatalogParityResult, ...]


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
) -> CatalogPreparation:
    """Reuse a matching snapshot or rebuild and atomically publish after DB parity."""

    root = Path(catalog_root).resolve()
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
        return CatalogPreparation(root, "rebuilt", master_sha, parity)
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
