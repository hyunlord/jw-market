"""Manifest schema for JW_Input_Detection_Contract_v2 (code canonical copy).

The contract document itself was not found in the repo, so this module encodes
the operative requirements quoted in the commissioning brief: the webhook
payload carries a manifest path (§3) and the manifest identifies one confirmed
submission (epoch + category + file list with sha256). If the canonical
document uses different field names, fix them HERE only — every other module
consumes the parsed ``Manifest`` object, never raw JSON.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

CONTRACT_VERSION = "v2"

# Submission epochs are period identifiers, not mart content hashes.
_EPOCH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2]|Q[1-4])$")


class ContractError(ValueError):
    """Manifest violates the input-detection contract."""


@dataclass(frozen=True)
class ManifestFile:
    path: str
    sha256: str
    rows: int | None = None
    period_start: str | None = None
    period_end: str | None = None


@dataclass(frozen=True)
class Manifest:
    contract_version: str
    epoch: str
    category: str
    complete: bool
    files: tuple[ManifestFile, ...]
    submitted_at: str | None = None
    manifest_path: str = ""
    manifest_sha: str = ""
    raw: dict = field(default_factory=dict, compare=False)


def _require(data: dict, key: str):
    if key not in data:
        raise ContractError(f"manifest missing required field {key!r}")
    return data[key]


def parse_manifest_bytes(payload: bytes, *, manifest_path: str = "") -> Manifest:
    """Parse and validate manifest bytes; fail closed on any contract gap."""
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"manifest is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError("manifest root must be a JSON object")

    version = str(_require(data, "contract_version"))
    if version != CONTRACT_VERSION:
        raise ContractError(f"unsupported contract_version {version!r}; expected {CONTRACT_VERSION!r}")

    epoch = str(_require(data, "epoch"))
    if not _EPOCH_RE.match(epoch):
        raise ContractError(f"epoch {epoch!r} is not YYYY-MM or YYYY-Qn")

    category = str(_require(data, "category")).strip().lower()
    if not category:
        raise ContractError("category must be non-empty")

    complete = _require(data, "complete")
    if not isinstance(complete, bool):
        raise ContractError("complete must be a JSON boolean")

    raw_files = _require(data, "files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ContractError("files must be a non-empty array")

    files: list[ManifestFile] = []
    for index, entry in enumerate(raw_files):
        if not isinstance(entry, dict):
            raise ContractError(f"files[{index}] must be an object")
        path = str(_require(entry, "path"))
        sha = str(_require(entry, "sha256")).lower()
        if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
            raise ContractError(f"files[{index}].sha256 is not a hex sha256")
        rows = entry.get("rows")
        if rows is not None and (not isinstance(rows, int) or rows < 0):
            raise ContractError(f"files[{index}].rows must be a non-negative integer")
        files.append(
            ManifestFile(
                path=path,
                sha256=sha,
                rows=rows,
                period_start=entry.get("period_start"),
                period_end=entry.get("period_end"),
            )
        )

    return Manifest(
        contract_version=version,
        epoch=epoch,
        category=category,
        complete=complete,
        files=tuple(files),
        submitted_at=data.get("submitted_at"),
        manifest_path=manifest_path,
        manifest_sha=hashlib.sha256(payload).hexdigest(),
        raw=data,
    )


def load_manifest(path: Path) -> Manifest:
    if not path.is_file():
        raise ContractError(f"manifest not found: {path}")
    return parse_manifest_bytes(path.read_bytes(), manifest_path=str(path))
