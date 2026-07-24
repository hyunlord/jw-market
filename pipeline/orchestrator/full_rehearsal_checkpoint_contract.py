"""Identity primitives for content-addressed R-1 checkpoints."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class CheckpointContractError(ValueError):
    """Raised when checkpoint identity or artifact census is incomplete."""


@dataclass(frozen=True)
class DatabaseCensus:
    row_count: int
    period_counts: Mapping[str, int]


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory_canonical_sha(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointContractError(f"cannot read canonical input inventory: {exc}") from exc
    return sha256_bytes(canonical_json(payload))


def build_checkpoint_identity(
    *,
    inventory_path: Path,
    image_digest: str,
    git_commit: str,
    normalized_stage_args: Mapping[str, object],
    nonsecret_config: Mapping[str, object],
    sidecar_manifest_sha: str,
) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
        raise CheckpointContractError("checkpoint identity requires a pinned image digest")
    if not re.fullmatch(r"[0-9a-f]{40}", git_commit):
        raise CheckpointContractError("checkpoint identity requires a full git commit")
    if not re.fullmatch(r"[0-9a-f]{64}", sidecar_manifest_sha):
        raise CheckpointContractError("checkpoint identity requires a sidecar manifest SHA256")
    payload = {
        "git_commit": git_commit,
        "image_digest": image_digest,
        "input_inventory_canonical_sha": inventory_canonical_sha(inventory_path),
        "nonsecret_config_hash": sha256_bytes(canonical_json(nonsecret_config)),
        "normalized_stage_args": normalized_stage_args,
        "sidecar_manifest_sha": sidecar_manifest_sha,
    }
    return sha256_bytes(canonical_json(payload))
