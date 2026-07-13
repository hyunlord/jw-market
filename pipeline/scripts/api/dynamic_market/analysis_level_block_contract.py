"""Shared storage contract for precomputed analysis-level blocks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence


ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION = "analysis-level-block-v4-filter-complete"


def channel_profile_signature(channels: Sequence[str]) -> str:
    payload = json.dumps([str(channel) for channel in channels], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def analysis_level_profile_signature(
    *,
    base_profile: str,
    dimension_filters: Sequence[tuple[str, Sequence[str]]],
) -> str:
    """Return the replay identity for every row filter that can change a block.

    Unfiltered requests retain the existing channel profile so the bounded
    precompute corpus stays market-sized. Filtered combinations use distinct
    identities and safely fall back to mart-direct unless explicitly baked.
    """

    canonical_filters = sorted(
        (
            str(dimension_type),
            sorted({str(value) for value in values if str(value)}),
        )
        for dimension_type, values in dimension_filters
        if any(str(value) for value in values)
    )
    if not canonical_filters:
        return base_profile
    payload = json.dumps(
        {"base_profile": base_profile, "dimension_filters": canonical_filters},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
