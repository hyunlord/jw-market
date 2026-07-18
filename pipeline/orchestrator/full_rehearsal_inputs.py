"""Materialize canonical raw inputs for an isolated full-pipeline rehearsal."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
from typing import Protocol
import unicodedata

from pipeline.etl.lib.storage import (
    MI_MASTER_DIR_NAME,
    MI_MASTER_FILE_NAME,
    PROJECT_ROOT,
)
from pipeline.scripts.ingest_hook.s3_input import S3Input


class InputMaterializationError(RuntimeError):
    """Raised when an R-1 source population cannot be materialized exactly."""


class ReadOnlyObjectStore(Protocol):
    def list_keys(self, prefix: str) -> list[str]: ...

    def read(self, key: str) -> bytes: ...


ClientFactory = Callable[[str], ReadOnlyObjectStore]
DEFAULT_MI_MASTER_SOURCE_DIR = PROJECT_ROOT / "data" / MI_MASTER_DIR_NAME
SOURCE_PINS_FILE_NAME = "SOURCE_PINS.sha256"


def materialize_full_inputs(
    *,
    output_root: Path,
    ubist_bucket: str,
    iqvia_bucket: str,
    mi_master_source_dir: Path | None = None,
    client_factory: ClientFactory | None = None,
) -> Path:
    """Materialize object-store raws plus the repository-pinned MI workbook."""
    root = output_root.resolve()
    if root.exists():
        raise InputMaterializationError(f"output root already exists: {root}")
    root.mkdir(parents=True)

    factory = client_factory or _client_from_env
    inventory: list[dict[str, str | int]] = []
    ubist_dir = root / "ubist"
    iqvia_dir = root / "iqvia"
    master_dir = root / "mi-master"

    _download_population(
        label="UBIST",
        bucket=ubist_bucket,
        destination=ubist_dir,
        suffixes=frozenset({".xlsx"}),
        client=factory(ubist_bucket),
        inventory=inventory,
    )
    _download_population(
        label="IQVIA",
        bucket=iqvia_bucket,
        destination=iqvia_dir,
        suffixes=frozenset({".csv", ".xls", ".xlsx"}),
        client=factory(iqvia_bucket),
        inventory=inventory,
    )
    master = _materialize_repository_mi_master(
        source_dir=mi_master_source_dir or DEFAULT_MI_MASTER_SOURCE_DIR,
        destination=master_dir,
        inventory=inventory,
    )

    manifest_path = root / "input_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ubist_source_dir": str(ubist_dir),
                "iqvia_source_dir": str(iqvia_dir),
                "mi_master": str(master),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    inventory.sort(key=lambda row: (str(row["bucket"]), str(row["key"])))
    (root / "input_inventory.json").write_text(
        json.dumps(
            {
                "classification": "census",
                "missing": "fail",
                "objects": inventory,
                "population": len(inventory),
                "schema_version": 1,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return manifest_path


def _download_population(
    *,
    label: str,
    bucket: str,
    destination: Path,
    suffixes: frozenset[str],
    client: ReadOnlyObjectStore,
    inventory: list[dict[str, str | int]],
) -> list[Path]:
    keys = [
        key
        for key in sorted(client.list_keys(""))
        if not Path(key).name.startswith(("~$", "._"))
        and Path(key).suffix.lower() in suffixes
    ]
    if not keys:
        raise InputMaterializationError(f"{label} bucket {bucket!r} has no supported objects")

    paths: list[Path] = []
    for key in keys:
        target = _safe_target(destination, key)
        payload = client.read(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        paths.append(target)
        inventory.append(
            {
                "bucket": bucket,
                "key": key,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        )
    return paths


def _safe_target(root: Path, key: str) -> Path:
    parts = Path(key.lstrip("/")).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise InputMaterializationError(f"object key escapes bucket namespace: {key!r}")
    target = (root / Path(*parts)).resolve()
    if root.resolve() not in target.parents:
        raise InputMaterializationError(f"object key escapes output root: {key!r}")
    return target


def _materialize_repository_mi_master(
    *,
    source_dir: Path,
    destination: Path,
    inventory: list[dict[str, str | int]],
) -> Path:
    source_root = source_dir.resolve()
    if not source_root.is_dir():
        raise InputMaterializationError(
            f"MI Master repository source directory is missing: {source_root}"
        )

    master = _select_mi_master(sorted(source_root.glob("*.xlsx")))
    expected_sha = _read_pinned_sha(source_root / SOURCE_PINS_FILE_NAME, master.name)
    payload = master.read_bytes()
    actual_sha = hashlib.sha256(payload).hexdigest()
    if actual_sha != expected_sha:
        raise InputMaterializationError(
            f"MI Master SHA256 mismatch: expected {expected_sha}, got {actual_sha}"
        )

    destination.mkdir(parents=True, exist_ok=True)
    target = destination / MI_MASTER_FILE_NAME
    target.write_bytes(payload)
    inventory.append(
        {
            "bucket": "repository",
            "key": MI_MASTER_FILE_NAME,
            "sha256": actual_sha,
            "size": len(payload),
        }
    )
    return target


def _read_pinned_sha(pin_path: Path, master_name: str) -> str:
    if not pin_path.is_file():
        raise InputMaterializationError(f"MI Master source pin is missing: {pin_path}")

    expected_name = unicodedata.normalize("NFC", master_name)
    matches: list[str] = []
    for line in pin_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(maxsplit=1)
        if len(parts) != 2:
            continue
        digest, name = parts
        if unicodedata.normalize("NFC", name.strip()) == expected_name:
            matches.append(digest.lower())
    if len(matches) != 1 or len(matches[0]) != 64 or any(
        char not in "0123456789abcdef" for char in matches[0]
    ):
        raise InputMaterializationError(
            f"MI Master source pin population must be exactly 1 valid SHA256; found {len(matches)}"
        )
    return matches[0]


def _select_mi_master(paths: list[Path]) -> Path:
    expected = unicodedata.normalize("NFC", MI_MASTER_FILE_NAME)
    matches = [path for path in paths if unicodedata.normalize("NFC", path.name) == expected]
    if len(matches) != 1:
        raise InputMaterializationError(
            f"MI Master canonical workbook population must be exactly 1; found {len(matches)}"
        )
    return matches[0]


def _client_from_env(bucket: str) -> S3Input:
    endpoint = os.environ.get("MINIO_ENDPOINT", "").strip()
    access_key = os.environ.get("MINIO_ACCESS_KEY", "")
    secret_key = os.environ.get("MINIO_SECRET_KEY", "")
    if not endpoint or not access_key or not secret_key:
        raise InputMaterializationError(
            "MINIO_ENDPOINT/MINIO_ACCESS_KEY/MINIO_SECRET_KEY are required"
        )
    return S3Input(
        endpoint=endpoint.rstrip("/"),
        bucket=bucket,
        access_key=access_key,
        secret_key=secret_key,
        region=os.environ.get("MINIO_REGION", "us-east-1"),
    )
