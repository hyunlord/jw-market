"""Materialize the immutable S4 catalog before an ingest mart build."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Final

from pipeline.etl.io.catalog.paths import (
    CatalogStorageAccessError,
    materialize_catalog,
    validate_catalog_materialization,
)
from pipeline.etl.lib.storage import sync_minio_to_local


ENV_CATALOG_BUCKET: Final = "INGEST_CATALOG_BUCKET"
ENV_CATALOG_PREFIX: Final = "INGEST_CATALOG_PREFIX"
S4_REQUIRED_CATALOGS: Final = frozenset({"strategic_brand", "strategic_product"})


def materialize_s4_catalog(catalog_root: Path) -> Path:
    """Materialize and validate the immutable catalog before S4 can start."""

    root = Path(catalog_root).resolve()
    bucket = os.environ.get(ENV_CATALOG_BUCKET, "").strip()
    prefix = os.environ.get(ENV_CATALOG_PREFIX, "").strip()
    if bool(bucket) != bool(prefix):
        raise CatalogStorageAccessError(
            f"{ENV_CATALOG_BUCKET} and {ENV_CATALOG_PREFIX} must be configured together"
        )
    if bucket:
        with tempfile.TemporaryDirectory(prefix="jw-market-catalog-s4-") as work:
            source_root = Path(work)
            try:
                sync_minio_to_local(
                    bucket,
                    prefix,
                    source_root,
                    overwrite=False,
                    progress=True,
                )
            except Exception as exc:  # noqa: BLE001 - normalize adapter failures at this boundary.
                raise CatalogStorageAccessError(
                    f"catalog storage access failed: {bucket}/{prefix}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            materialize_catalog(
                source_root=source_root,
                destination_root=root,
                required_names=S4_REQUIRED_CATALOGS,
            )
    validate_catalog_materialization(root, required_names=S4_REQUIRED_CATALOGS)
    return root
