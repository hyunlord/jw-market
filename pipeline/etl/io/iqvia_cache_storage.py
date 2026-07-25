"""Storage boundary for IQVIA parquet caches."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Protocol

from pipeline.etl.io.iqvia_minio_cache_storage import MinioCacheStorage


class CacheStorage(Protocol):
    def exists(self, key: str) -> bool: ...

    def get_bytes(self, key: str) -> bytes: ...

    def put_bytes(self, key: str, content: bytes) -> None: ...

    def put_file(self, key: str, source: Path) -> None: ...

    def get_file(self, key: str, destination: Path) -> None: ...

    def size(self, key: str) -> int: ...

    def list_keys(self, prefix: str) -> tuple[str, ...]: ...


class LocalCacheStorage:
    """Filesystem cache adapter with object-store-like key semantics."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        candidate = PurePosixPath(key)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"invalid cache object key: {key!r}")
        path = self._root.joinpath(*candidate.parts).resolve()
        if path != self._root and self._root not in path.parents:
            raise ValueError(f"cache object escapes root: {key!r}")
        return path

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def put_bytes(self, key: str, content: bytes) -> None:
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temp:
            temp.write(content)
            temp_path = Path(temp.name)
        os.replace(temp_path, destination)

    def put_file(self, key: str, source: Path) -> None:
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temp:
            temp_path = Path(temp.name)
        shutil.copyfile(source, temp_path)
        os.replace(temp_path, destination)

    def get_file(self, key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._path(key), destination)

    def size(self, key: str) -> int:
        return self._path(key).stat().st_size

    def list_keys(self, prefix: str) -> tuple[str, ...]:
        root = self._path(prefix)
        if not root.exists():
            return ()
        return tuple(
            sorted(
                path.relative_to(self._root).as_posix()
                for path in root.rglob("*")
                if path.is_file()
            )
        )


def build_iqvia_minio_cache_storage() -> MinioCacheStorage:
    """Build the IQVIA parquet cache's MinIO adapter from its own dedicated env vars.

    Deliberately does not read the generic ``MINIO_ACCESS_KEY``/``MINIO_SECRET_KEY``
    vars used elsewhere (e.g. the archival UBIST/IQVIA sync in
    ``pipeline/etl/lib/storage.py``) so this cache's credential — scoped to a single
    bucket prefix — can never be conflated with a broader-access credential in the
    same process.
    """
    endpoint = os.environ.get("IQVIA_CACHE_MINIO_ENDPOINT")
    access_key = os.environ.get("IQVIA_CACHE_MINIO_ACCESS_KEY")
    secret_key = os.environ.get("IQVIA_CACHE_MINIO_SECRET_KEY")
    bucket = os.environ.get("IQVIA_CACHE_MINIO_BUCKET", "jw-market-raw")
    root_prefix = os.environ.get("IQVIA_CACHE_MINIO_PREFIX", "iqvia-parquet-cache")

    missing = [
        name
        for name, value in (
            ("IQVIA_CACHE_MINIO_ENDPOINT", endpoint),
            ("IQVIA_CACHE_MINIO_ACCESS_KEY", access_key),
            ("IQVIA_CACHE_MINIO_SECRET_KEY", secret_key),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"IQVIA cache MinIO credentials missing: {missing}")
    assert endpoint is not None
    assert access_key is not None
    assert secret_key is not None

    return MinioCacheStorage(
        endpoint=endpoint,
        bucket=bucket,
        access_key=access_key,
        secret_key=secret_key,
        root_prefix=root_prefix,
    )


__all__ = [
    "CacheStorage",
    "LocalCacheStorage",
    "MinioCacheStorage",
    "build_iqvia_minio_cache_storage",
]
