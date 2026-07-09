from __future__ import annotations

import re

BRAND_SPACE_RE = re.compile(r"\s+")


def compact_brand_name(value: object) -> str:
    """Return a whitespace-free brand comparison key."""

    return BRAND_SPACE_RE.sub("", str(value or "").strip())
