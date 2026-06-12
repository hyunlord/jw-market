"""File-manifest utilities for s0 verify.

s0 intentionally stops at file identity: path, size, mtime, and SHA-256 over
raw bytes. It does not parse workbook/CSV contents, infer periods, or validate
five-year coverage. Many source file names do not carry enough period metadata,
so coverage checks belong to s1 load after content parsing.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

SOURCE_NAMES = {
    "MI Master": "MIMASTER",
    "UBIST dir": "UBIST",
    "IQVIA dir": "IQVIA",
    "Target priority skeleton": "SKELETON",
}
SOURCE_SUFFIXES = {".xlsx", ".csv"}


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_row(source: str, root: Path, path: Path, recorded_at: datetime) -> dict[str, Any]:
    stat = path.stat()
    file_name = path.relative_to(root).as_posix() if root.is_dir() else path.name
    return {
        "source": source,
        "file_name": file_name,
        "file_hash": _hash_file(path),
        "file_size": stat.st_size,
        "mtime": stat.st_mtime,
        "recorded_at": recorded_at,
    }


def scan_source_files(required: dict[str, Path]) -> list[dict[str, Any]]:
    """Return file fingerprints for the four verified source groups.

    Directories are expanded to individual ``.xlsx``/``.csv`` entries. Files are
    opened only as bytes for hashing; no source content parsing, period
    extraction, or row counting is performed in s0.
    """
    recorded_at = datetime.now()
    rows: list[dict[str, Any]] = []
    for label, root in required.items():
        source = SOURCE_NAMES[label]
        if root.is_dir():
            files = sorted(
                file
                for file in root.rglob("*")
                if file.is_file() and file.suffix.lower() in SOURCE_SUFFIXES
            )
            rows.extend(_manifest_row(source, root, file, recorded_at) for file in files)
        elif root.is_file():
            rows.append(_manifest_row(source, root, root, recorded_at))
    return rows


def compare(prev: dict[tuple[str, str], dict[str, Any]], cur: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare previous and current manifests without making skip/load decisions."""
    current = {(row["source"], row["file_name"]): row for row in cur}
    previous_keys = set(prev)
    current_keys = set(current)
    new_keys = sorted(current_keys - previous_keys)
    missing_keys = sorted(previous_keys - current_keys)
    changed_keys = sorted(
        key for key in previous_keys & current_keys if prev[key]["file_hash"] != current[key]["file_hash"]
    )
    return {
        "identical": not new_keys and not missing_keys and not changed_keys,
        "new_files": [current[key] for key in new_keys],
        "missing_files": [prev[key] for key in missing_keys],
        "changed_files": [current[key] for key in changed_keys],
    }
