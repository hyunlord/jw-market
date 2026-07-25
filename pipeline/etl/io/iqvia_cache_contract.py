"""Immutable contracts for content-addressed IQVIA parquet caches."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CACHE_FORMAT_VERSION = 1
CACHE_PREFIX = "iqvia/nsa/parquet-cache/v1"


class CacheUnavailableError(RuntimeError):
    """Raised when no completed cache exists for the requested source."""


class CacheIntegrityError(RuntimeError):
    """Raised when a completed cache does not match its manifest."""


@dataclass(frozen=True, slots=True)
class CacheIdentity:
    source_sha256: str
    schema_revision: str
    cache_key: str

    @property
    def cache_prefix(self) -> str:
        return f"{CACHE_PREFIX}/{self.cache_key}"


@dataclass(frozen=True, slots=True)
class CacheFile:
    key: str
    size_bytes: int
    sha256: str
    row_count: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CacheFile:
        return cls(
            key=str(value["key"]),
            size_bytes=int(value["size_bytes"]),
            sha256=str(value["sha256"]),
            row_count=int(value["row_count"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "row_count": self.row_count,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class CachePartition:
    quarter: str
    atc4_code: str
    row_count: int
    sha256: str
    files: tuple[CacheFile, ...]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CachePartition:
        files = tuple(CacheFile.from_dict(item) for item in value["files"])
        return cls(
            quarter=str(value["quarter"]),
            atc4_code=str(value["atc4_code"]),
            row_count=int(value["row_count"]),
            sha256=str(value["sha256"]),
            files=files,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "atc4_code": self.atc4_code,
            "files": [item.to_dict() for item in self.files],
            "quarter": self.quarter,
            "row_count": self.row_count,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class CacheManifest:
    cache_key: str
    schema_revision: str
    source_name: str
    source_sha256: str
    total_rows: int
    partitions: tuple[CachePartition, ...]
    format_version: int = CACHE_FORMAT_VERSION

    @property
    def cache_prefix(self) -> str:
        return f"{CACHE_PREFIX}/{self.cache_key}"

    @classmethod
    def from_bytes(cls, content: bytes) -> CacheManifest:
        try:
            value = json.loads(content)
            partitions = tuple(
                CachePartition.from_dict(item) for item in value["partitions"]
            )
            manifest = cls(
                cache_key=str(value["cache_key"]),
                schema_revision=str(value["schema_revision"]),
                source_name=str(value["source_name"]),
                source_sha256=str(value["source_sha256"]),
                total_rows=int(value["total_rows"]),
                partitions=partitions,
                format_version=int(value["format_version"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CacheIntegrityError("invalid IQVIA cache manifest") from exc
        if manifest.format_version != CACHE_FORMAT_VERSION:
            raise CacheIntegrityError(
                f"unsupported IQVIA cache format: {manifest.format_version}"
            )
        return manifest

    def to_bytes(self) -> bytes:
        value = {
            "cache_key": self.cache_key,
            "format_version": self.format_version,
            "partitions": [item.to_dict() for item in self.partitions],
            "schema_revision": self.schema_revision,
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "total_rows": self.total_rows,
        }
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_cache_identity(source_sha256: str, schema_revision: str) -> CacheIdentity:
    material = f"{source_sha256}\n{schema_revision}\n".encode("ascii")
    return CacheIdentity(
        source_sha256=source_sha256,
        schema_revision=schema_revision,
        cache_key=sha256_bytes(material),
    )


def partition_digest(files: tuple[CacheFile, ...]) -> str:
    material = "".join(
        f"{item.key}\t{item.row_count}\t{item.size_bytes}\t{item.sha256}\n"
        for item in files
    )
    return sha256_bytes(material.encode("utf-8"))
