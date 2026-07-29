"""Schema, identity, and integrity checks for IQVIA parquet caches."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa

from pipeline.etl.io.iqvia_cache_contract import (
    CacheIdentity,
    CacheIntegrityError,
    CacheManifest,
    CacheUnavailableError,
    make_cache_identity,
    partition_digest,
    sha256_bytes,
    sha256_file,
)
from pipeline.etl.io.iqvia_cache_storage import CacheStorage
from pipeline.etl.io.iqvia_loader import record_parquet_schema


MANIFEST_NAME = "manifest.json"
SUCCESS_NAME = "_SUCCESS"
SEQUENCE_COLUMN = "_cache_sequence"


def cache_parquet_schema() -> pa.Schema:
    return pa.schema(
        [*record_parquet_schema(), pa.field(SEQUENCE_COLUMN, pa.int64())]
    )


def cache_schema_revision() -> str:
    schema = cache_parquet_schema()
    value = {
        "fields": [
            {"name": field.name, "nullable": field.nullable, "type": str(field.type)}
            for field in schema
        ],
        "partitioning": "quarter/atc4",
        "revision": 1,
    }
    content = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return sha256_bytes(content)


def cache_identity_for_source(source: Path) -> CacheIdentity:
    return make_cache_identity(sha256_file(source), cache_schema_revision())


def manifest_keys(identity: CacheIdentity) -> tuple[str, str]:
    return (
        f"{identity.cache_prefix}/{MANIFEST_NAME}",
        f"{identity.cache_prefix}/{SUCCESS_NAME}",
    )


def load_verified_manifest(
    identity: CacheIdentity,
    storage: CacheStorage,
) -> CacheManifest:
    manifest_key, success_key = manifest_keys(identity)
    if not storage.exists(success_key):
        raise CacheUnavailableError(
            f"IQVIA parquet cache has no {SUCCESS_NAME}: {identity.cache_key}"
        )
    if not storage.exists(manifest_key):
        raise CacheIntegrityError(
            f"IQVIA parquet cache has {SUCCESS_NAME} without {MANIFEST_NAME}"
        )
    manifest_bytes = storage.get_bytes(manifest_key)
    marker = storage.get_bytes(success_key).decode("ascii").strip()
    if marker != sha256_bytes(manifest_bytes):
        raise CacheIntegrityError("IQVIA cache manifest checksum mismatch")
    manifest = CacheManifest.from_bytes(manifest_bytes)
    if (
        manifest.cache_key != identity.cache_key
        or manifest.source_sha256 != identity.source_sha256
        or manifest.schema_revision != identity.schema_revision
    ):
        raise CacheIntegrityError("IQVIA cache identity does not match source")
    if manifest.total_rows != sum(item.row_count for item in manifest.partitions):
        raise CacheIntegrityError("IQVIA cache manifest row count mismatch")
    for partition in manifest.partitions:
        if partition.row_count != sum(item.row_count for item in partition.files):
            raise CacheIntegrityError("IQVIA cache partition row count mismatch")
        if partition.sha256 != partition_digest(partition.files):
            raise CacheIntegrityError("IQVIA cache partition checksum mismatch")
        for item in partition.files:
            if not storage.exists(item.key):
                raise CacheIntegrityError(f"IQVIA cache object missing: {item.key}")
            if storage.size(item.key) != item.size_bytes:
                raise CacheIntegrityError(
                    f"IQVIA cache object checksum/size mismatch: {item.key}"
                )
    return manifest
