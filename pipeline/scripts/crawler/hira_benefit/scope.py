from __future__ import annotations

import json
import unicodedata
from collections.abc import Sequence


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def brands_from_cache_payload(response_json: str) -> tuple[str, ...]:
    """Return JW brand names from cache_brands.default without a code-side list."""

    payload = json.loads(response_json)
    if not isinstance(payload, list):
        raise TypeError("cache_brands.response_json must be a list")
    result: list[str] = []
    for row in payload:
        if not isinstance(row, dict) or not bool(row.get("is_jw")):
            continue
        name = str(row.get("brand") or "").strip()
        if name:
            result.append(name)
    if not result:
        raise ValueError("cache_brands contains no is_jw brands")
    return tuple(dict.fromkeys(result))


def match_brand_names(raw_text: str, brand_names: Sequence[str]) -> tuple[str, ...]:
    """Match explicit names, longest first, preserving all independently present names."""

    normalized_text = _normalize(raw_text)
    ordered = sorted(
        (name for name in brand_names if name.strip()),
        key=lambda name: (-len(_normalize(name)), _normalize(name)),
    )
    return tuple(name for name in ordered if _normalize(name) in normalized_text)
