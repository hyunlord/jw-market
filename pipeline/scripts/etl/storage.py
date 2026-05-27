"""Storage helpers for ETL local/MinIO workflows.

This module is intentionally unused by the current ETL loaders until the
follow-up integration phase. The default backend is local so importing this
helper cannot change existing ETL behavior.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BACKEND_LOCAL = "local"
BACKEND_MINIO = "minio"
VALID_BACKENDS = {BACKEND_LOCAL, BACKEND_MINIO}
DEFAULT_WORK_DIR = Path("/tmp/jw-market-etl")


def get_storage_backend() -> str:
    """Return the configured storage backend.

    ``ETL_STORAGE_BACKEND`` accepts ``local`` or ``minio``. Missing values
    default to ``local`` as the safe fallback.
    """
    backend = os.environ.get("ETL_STORAGE_BACKEND", BACKEND_LOCAL).strip().lower()
    if backend not in VALID_BACKENDS:
        raise ValueError(
            f"ETL_STORAGE_BACKEND={backend!r} is invalid. "
            f"Expected one of {sorted(VALID_BACKENDS)}."
        )
    return backend


def is_minio_backend() -> bool:
    """Return true when the ETL should stage data from MinIO."""
    return get_storage_backend() == BACKEND_MINIO


def is_local_backend() -> bool:
    """Return true when the ETL should use repository-local paths."""
    return get_storage_backend() == BACKEND_LOCAL


def _require_minio_env() -> tuple[str, str, str, bool]:
    endpoint = os.environ.get("MINIO_ENDPOINT")
    access_key = os.environ.get("MINIO_ACCESS_KEY")
    secret_key = os.environ.get("MINIO_SECRET_KEY")
    secure = os.environ.get("MINIO_SECURE", "false").strip().lower() == "true"

    missing = [
        key
        for key, value in (
            ("MINIO_ENDPOINT", endpoint),
            ("MINIO_ACCESS_KEY", access_key),
            ("MINIO_SECRET_KEY", secret_key),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"MinIO credentials missing: {missing}. "
            "Set the env vars or use ETL_STORAGE_BACKEND=local."
        )

    assert endpoint is not None
    assert access_key is not None
    assert secret_key is not None
    if not endpoint.startswith(("http://", "https://")):
        scheme = "https" if secure else "http"
        endpoint = f"{scheme}://{endpoint}"

    return endpoint, access_key, secret_key, secure


def create_minio_client() -> Any:
    """Create a boto3 S3 client configured for MinIO."""
    endpoint, access_key, secret_key, _secure = _require_minio_env()

    try:
        import boto3
        from botocore.client import Config
    except ImportError as exc:
        raise RuntimeError(
            "boto3 is required for MinIO backend. Install it with: pip install boto3"
        ) from exc

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(signature_version="s3v4"),
    )


def _relative_object_key(key: str, prefix: str) -> str:
    if prefix and key.startswith(prefix):
        return key[len(prefix) :].lstrip("/")
    return key.lstrip("/")


def sync_minio_to_local(
    bucket: str,
    prefix: str,
    local_dir: Path,
    *,
    overwrite: bool = False,
    progress: bool = True,
    client: Any | None = None,
) -> int:
    """Download objects under ``bucket/prefix`` into ``local_dir``.

    Existing files are skipped unless ``overwrite`` is true. The return value is
    the number of downloaded files.
    """
    s3 = client or create_minio_client()
    destination = Path(local_dir)
    destination.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            relative_key = _relative_object_key(key, prefix)
            if not relative_key:
                continue

            target_path = destination / relative_key
            if target_path.exists() and not overwrite:
                logger.debug("skip existing MinIO object: %s", target_path)
                continue

            target_path.parent.mkdir(parents=True, exist_ok=True)
            if progress:
                logger.info("download %s/%s -> %s", bucket, key, target_path)
            s3.download_file(bucket, key, str(target_path))
            downloaded += 1

    logger.info("downloaded %s files from %s/%s", downloaded, bucket, prefix)
    return downloaded


def upload_local_to_minio(
    local_dir: Path,
    bucket: str,
    prefix: str,
    *,
    progress: bool = True,
    client: Any | None = None,
) -> int:
    """Upload every file under ``local_dir`` to ``bucket/prefix``."""
    s3 = client or create_minio_client()
    source = Path(local_dir)
    if not source.exists():
        raise FileNotFoundError(f"local dir not found: {source}")

    uploaded = 0
    for file_path in source.rglob("*"):
        if not file_path.is_file():
            continue

        relative_path = file_path.relative_to(source).as_posix()
        key = f"{prefix.rstrip('/')}/{relative_path}".lstrip("/")
        if progress:
            logger.info("upload %s -> %s/%s", file_path, bucket, key)
        s3.upload_file(str(file_path), bucket, key)
        uploaded += 1

    logger.info("uploaded %s files to %s/%s", uploaded, bucket, prefix)
    return uploaded


def get_work_dir() -> Path:
    """Return the local staging directory for MinIO-backed ETL runs."""
    return Path(os.environ.get("ETL_WORK_DIR", str(DEFAULT_WORK_DIR)))


def get_data_path(
    bucket_env: str,
    bucket_default: str,
    local_default: Path,
    *,
    prefix: str = "",
    work_subdir: str | None = None,
    overwrite: bool = False,
    progress: bool = True,
    client: Any | None = None,
) -> Path:
    """Resolve a local path for ETL input.

    Local backend returns ``local_default`` directly. MinIO backend downloads
    ``bucket/prefix`` into ``ETL_WORK_DIR/work_subdir`` and returns that staged
    directory.
    """
    if is_local_backend():
        return Path(local_default)

    bucket = os.environ.get(bucket_env, bucket_default)
    local_dir = get_work_dir() / (work_subdir or bucket)
    sync_minio_to_local(
        bucket,
        prefix,
        local_dir,
        overwrite=overwrite,
        progress=progress,
        client=client,
    )
    return local_dir
