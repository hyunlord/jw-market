"""Consume fail-closed IQVIA parquet caches."""

from __future__ import annotations

import heapq
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator

import pyarrow.parquet as pq

from pipeline.etl.io.iqvia_cache_contract import (
    CacheIdentity,
    CacheIntegrityError,
    CacheUnavailableError,
    make_cache_identity,
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
    max_rows: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Read only a verified cache; never fall back to the workbook."""
    identity = cache_identity_for_source(source)
    yield from _iter_iqvia_parquet_cache_identity(
        identity,
        storage,
        quarters=quarters,
        atc4_codes=atc4_codes,
        batch_size=batch_size,
        max_rows=max_rows,
    )


def iter_iqvia_parquet_cache_for_source_sha256(
    source_sha256: str,
    storage: CacheStorage,
    *,
    quarters: Iterable[str] | None = None,
    atc4_codes: Iterable[str] | None = None,
    batch_size: int = 10_000,
    max_rows: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Read a verified cache by approved source digest without opening the XLSX."""
    identity = make_cache_identity(source_sha256, cache_schema_revision())
    yield from _iter_iqvia_parquet_cache_identity(
        identity,
        storage,
        quarters=quarters,
        atc4_codes=atc4_codes,
        batch_size=batch_size,
        max_rows=max_rows,
    )


def available_iqvia_cache_atc4_codes_for_source_sha256(
    source_sha256: str,
    storage: CacheStorage,
    *,
    quarters: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Return ATC4 codes from a verified cache manifest without another source."""
    identity = make_cache_identity(source_sha256, cache_schema_revision())
    quarter_scope = normalize_iqvia_quarters(quarters)
    manifest = load_verified_manifest(identity, storage)
    return tuple(
        sorted(
            {
                partition.atc4_code
                for partition in manifest.partitions
                if not quarter_scope or partition.quarter in quarter_scope
            }
        )
    )


def available_iqvia_cache_quarters_for_source_sha256(
    source_sha256: str,
    storage: CacheStorage,
) -> tuple[str, ...]:
    """Return quarter labels from a verified cache manifest."""
    identity = make_cache_identity(source_sha256, cache_schema_revision())
    manifest = load_verified_manifest(identity, storage)
    return tuple(sorted({partition.quarter for partition in manifest.partitions}))


def _iter_iqvia_parquet_cache_identity(
    identity: CacheIdentity,
    storage: CacheStorage,
    *,
    quarters: Iterable[str] | None = None,
    atc4_codes: Iterable[str] | None = None,
    batch_size: int = 10_000,
    max_rows: int | None = None,
) -> Iterator[dict[str, Any]]:
    quarter_scope = normalize_iqvia_quarters(quarters)
    atc4_scope = normalize_iqvia_atc4_codes(atc4_codes)
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
        planned_rows = 0
        for partition in selected:
            for item in partition.files:
                local_path = staging / f"part-{len(paths):05d}.parquet"
                storage.get_file(item.key, local_path)
                if sha256_file(local_path) != item.sha256:
                    raise CacheIntegrityError(
                        f"IQVIA cache object checksum mismatch: {item.key}"
                    )
                paths.append(local_path)
                planned_rows += item.row_count
                if max_rows is not None and planned_rows >= max_rows:
                    break
            if max_rows is not None and planned_rows >= max_rows:
                break
        streams = (
            _iter_cache_file(path, batch_size=batch_size)
            for path in paths
        )
        yielded = 0
        for _, record in heapq.merge(*streams, key=lambda item: item[0]):
            if iqvia_record_in_scope(
                record,
                quarters=quarter_scope,
                atc4_codes=atc4_scope,
            ):
                yield record
                yielded += 1
                if max_rows is not None and yielded >= max_rows:
                    return


__all__ = [
    "CacheIntegrityError",
    "CacheUnavailableError",
    "LocalCacheStorage",
    "MinioCacheStorage",
    "build_iqvia_minio_cache_storage",
    "build_iqvia_parquet_cache",
    "available_iqvia_cache_atc4_codes_for_source_sha256",
    "available_iqvia_cache_quarters_for_source_sha256",
    "cache_identity_for_source",
    "cache_schema_revision",
    "iter_iqvia_parquet_cache",
    "iter_iqvia_parquet_cache_for_source_sha256",
]
