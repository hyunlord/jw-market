"""Build content-addressed IQVIA parquet caches."""

from __future__ import annotations

import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pyarrow as pa
import pyarrow.parquet as pq

from pipeline.etl.io.iqvia_cache_contract import (
    CacheFile,
    CacheIdentity,
    CacheManifest,
    CachePartition,
    partition_digest,
    sha256_bytes,
    sha256_file,
)
from pipeline.etl.io.iqvia_cache_format import (
    SEQUENCE_COLUMN,
    cache_identity_for_source,
    cache_parquet_schema,
    load_verified_manifest,
    manifest_keys,
)
from pipeline.etl.io.iqvia_cache_storage import CacheStorage
from pipeline.etl.io.iqvia_loader import (
    iter_nsa_xlsx,
    normalize_record_for_parquet,
    period_label_to_quarter,
)
from pipeline.etl.io.iqvia_scope import iqvia_record_atc4_code


def _object_key(
    identity: CacheIdentity,
    quarter: str,
    atc4_code: str,
    part_index: int,
) -> str:
    encoded_quarter = quote(quarter, safe="")
    encoded_atc4 = quote(atc4_code, safe="")
    return (
        f"{identity.cache_prefix}/data/quarter={encoded_quarter}/"
        f"atc4={encoded_atc4}/part-{part_index:05d}.parquet"
    )


def _flush_partition(
    *,
    identity: CacheIdentity,
    partition: tuple[str, str],
    rows: list[dict[str, Any]],
    part_index: int,
    staging: Path,
    storage: CacheStorage,
) -> CacheFile:
    quarter, atc4_code = partition
    key = _object_key(identity, quarter, atc4_code, part_index)
    filename = (
        f"part-{quote(quarter, safe='')}-{quote(atc4_code, safe='')}-"
        f"{part_index:05d}.parquet"
    )
    local_path = staging / filename
    pq.write_table(
        pa.Table.from_pylist(rows, schema=cache_parquet_schema()),
        local_path,
        compression="snappy",
    )
    content_sha256 = sha256_file(local_path)
    size_bytes = local_path.stat().st_size
    storage.put_file(key, local_path)
    return CacheFile(
        key=key,
        size_bytes=size_bytes,
        sha256=content_sha256,
        row_count=len(rows),
    )


def build_iqvia_parquet_cache(
    source: Path,
    storage: CacheStorage,
    *,
    batch_size: int = 10_000,
) -> CacheManifest:
    """Build a cache, publishing its success marker only after its manifest."""
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    identity = cache_identity_for_source(source)
    _, success_key = manifest_keys(identity)
    if storage.exists(success_key):
        return load_verified_manifest(identity, storage)

    buffers: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    files: dict[tuple[str, str], list[CacheFile]] = defaultdict(list)
    part_indexes: dict[tuple[str, str], int] = defaultdict(int)
    buffered_rows = 0
    total_rows = 0
    with tempfile.TemporaryDirectory(prefix="iqvia-cache-build-") as temp_dir:
        staging = Path(temp_dir)
        for sequence, record in enumerate(iter_nsa_xlsx(source)):
            partition = (
                period_label_to_quarter(record.get("period_label")),
                iqvia_record_atc4_code(record),
            )
            row = normalize_record_for_parquet(record)
            row[SEQUENCE_COLUMN] = sequence
            buffers[partition].append(row)
            buffered_rows += 1
            total_rows += 1
            if buffered_rows >= batch_size:
                selected = min(buffers, key=lambda key: (-len(buffers[key]), key))
                selected_rows = buffers.pop(selected)
                item = _flush_partition(
                    identity=identity,
                    partition=selected,
                    rows=selected_rows,
                    part_index=part_indexes[selected],
                    staging=staging,
                    storage=storage,
                )
                files[selected].append(item)
                part_indexes[selected] += 1
                buffered_rows -= len(selected_rows)
        for partition in sorted(buffers):
            rows = buffers[partition]
            if not rows:
                continue
            item = _flush_partition(
                identity=identity,
                partition=partition,
                rows=rows,
                part_index=part_indexes[partition],
                staging=staging,
                storage=storage,
            )
            files[partition].append(item)

    partitions = tuple(
        CachePartition(
            quarter=quarter,
            atc4_code=atc4_code,
            row_count=sum(item.row_count for item in partition_files),
            sha256=partition_digest(tuple(partition_files)),
            files=tuple(partition_files),
        )
        for (quarter, atc4_code), partition_files in sorted(files.items())
    )
    manifest = CacheManifest(
        cache_key=identity.cache_key,
        schema_revision=identity.schema_revision,
        source_name=source.name,
        source_sha256=identity.source_sha256,
        total_rows=total_rows,
        partitions=partitions,
    )
    manifest_bytes = manifest.to_bytes()
    manifest_key, success_key = manifest_keys(identity)
    storage.put_bytes(manifest_key, manifest_bytes)
    storage.put_bytes(success_key, (sha256_bytes(manifest_bytes) + "\n").encode("ascii"))
    return load_verified_manifest(identity, storage)
