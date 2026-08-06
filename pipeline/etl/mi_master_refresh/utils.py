"""Shared helpers for MI Master refresh gates."""

from __future__ import annotations

import hashlib
import json
import re


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def require_sha256(value: str, field: str) -> None:
    if not SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase sha256 hex digest")


def sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
