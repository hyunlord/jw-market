"""Shared storage contract for precomputed analysis-level blocks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence


ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION = "analysis-level-block-v3-ordered-profile"


def channel_profile_signature(channels: Sequence[str]) -> str:
    payload = json.dumps([str(channel) for channel in channels], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
