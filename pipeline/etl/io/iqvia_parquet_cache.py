"""Consume fail-closed IQVIA parquet caches."""

from __future__ import annotations

import heapq
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator

import pyarrow.parquet as pq

from pipeline.etl.io.iqvia_cache_contract import (
    CacheIntegrityError,
    CacheUnavailableError,
    sha256_file,
)
from pipeline.etl.io.iqvia_cache_builder import build_iqvia_parquet_cache
from pipeline.etl.io.iqvia_cache_format import (
    SEQUENCE_COLUMN,
    cache_identity_for_source,
    cache_schema_revision,
    load_verified_manifest,
)
from pipeline.etl.io.iqvia_cache_storage import (
    CacheStorage,
    LocalCacheStorage,
    MinioCacheStorage,
    build_iqvia_minio_cache_storage,
)
from pipeline.etl.io.iqvia_loader import RECORD_PARQUET_COLUMNS
from pipeline.etl.io.iqvia_scope import (
    iqvia_record_in_scope,
    normalize_iqvia_atc4_codes,
    normalize_iqvia_quarters,
)

def _iter_cache_file(
    path: Path,
    *,
    batch_size: int,
) -> Iterator[tuple[int, dict[str, Any]]]:
    columns = [*RECORD_PARQUET_COLUMNS, SEQUENCE_COLUMN]
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(batch_size=batch_size, columns=columns):
        for row in batch.to_pylist():
            sequence = int(row.pop(SEQUENCE_COLUMN))
            yield sequence, row


def iter_iqvia_parquet_cache(
    source: Path,
    storage: CacheStorage,
    *,
    quarters: Iterable[str] | None = None,
    atc4_codes: Iterable[str] | None = None,
    batch_size: int = 10_000,
) -> Iterator[dict[str, Any]]:
    """Read only a verified cache; never fall back to the workbook."""
    quarter_scope = normalize_iqvia_quarters(quarters)
    atc4_scope = normalize_iqvia_atc4_codes(atc4_codes)
    identity = cache_identity_for_source(source)
    manifest = load_verified_manifest(identity, storage)
    selected = (
        partition
        for partition in manifest.partitions
        if (not quarter_scope or partition.quarter in quarter_scope)
        and (not atc4_scope or partition.atc4_code in atc4_scope)
    )
    with tempfile.TemporaryDirectory(prefix="iqvia-cache-read-") as temp_dir:
        staging = Path(temp_dir)
        paths: list[Path] = []
        for partition in selected:
            for index, item in enumerate(partition.files):
                local_path = staging / f"part-{len(paths):05d}.parquet"
                storage.get_file(item.key, local_path)
                if sha256_file(local_path) != item.sha256:
                    raise CacheIntegrityError(
                        f"IQVIA cache object checksum mismatch: {item.key}"
                    )
                paths.append(local_path)
        streams = (
            _iter_cache_file(path, batch_size=batch_size)
            for path in paths
        )
        for _, record in heapq.merge(*streams, key=lambda item: item[0]):
            if iqvia_record_in_scope(
                record,
                quarters=quarter_scope,
                atc4_codes=atc4_scope,
            ):
                yield record


__all__ = [
    "CacheIntegrityError",
    "CacheUnavailableError",
    "LocalCacheStorage",
    "MinioCacheStorage",
    "build_iqvia_minio_cache_storage",
    "build_iqvia_parquet_cache",
    "cache_identity_for_source",
    "cache_schema_revision",
    "iter_iqvia_parquet_cache",
]
