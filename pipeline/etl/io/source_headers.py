"""Shared source-header normalization for raw workbook loaders."""

from __future__ import annotations

import re
import unicodedata


def normalize_source_header(value: object) -> str | None:
    """Return a compatibility key for source headers without changing cell data."""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value))
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold() or None
