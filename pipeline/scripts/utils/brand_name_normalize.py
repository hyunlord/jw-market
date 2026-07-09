"""Brand display-name normalization helpers."""

from __future__ import annotations

import re


def compact_brand_name(value: object) -> str:
    """Return a whitespace-insensitive brand key for lookup fallback."""

    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value).strip())
