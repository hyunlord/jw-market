#!/usr/bin/env python3
"""Brand-key normalization for source-aware JSON Layer 3 ETL v3."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

try:
    import pandas as pd
except Exception:  # pragma: no cover - pandas is present in the pipeline env.
    pd = None  # type: ignore[assignment]


DOSAGE_TOKENS = [
    "정",
    "정제",
    "캡슐",
    "캡슐제",
    "연질캡슐",
    "소프트캡슐",
    "주",
    "주사",
    "주사제",
    "시럽",
    "현탁액",
    "액",
    "크림",
    "겔",
    "연고",
    "패취",
    "패치",
    "tab",
    "tabs",
    "tablet",
    "tablets",
    "cap",
    "caps",
    "capsule",
    "capsules",
    "inj",
    "injection",
    "syr",
    "syrup",
]

UNIT_RE = re.compile(r"(\d+(\.\d+)?)\s*(mg|mcg|g|kg|ml|l|iu|%)", re.IGNORECASE)
BRACKET_RE = re.compile(r"\[[^\]]+\]|\([^)]*\)")
PUNCT_RE = re.compile(r"[^\w가-힣]+", re.UNICODE)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if pd is not None:
        try:
            return bool(pd.isna(value))
        except Exception:
            return False
    return False


def normalize_brand_name(raw_name: Any) -> str:
    """Return a deterministic brand key while preserving Hangul.

    The key is intentionally conservative: it removes dosage/form noise and
    punctuation, but it does not attempt lossy Hangul romanization. Strategic
    matching primarily uses catalog product/brand ids; this key is the stable
    bridge between general and strategic dry-run rows.
    """

    if _is_missing(raw_name):
        return ""

    text = unicodedata.normalize("NFKC", str(raw_name)).strip().lower()
    if text in {"", "nan", "none", "null"}:
        return ""

    text = BRACKET_RE.sub(" ", text)
    text = UNIT_RE.sub(" ", text)
    for token in DOSAGE_TOKENS:
        text = re.sub(rf"(?<![가-힣a-z0-9]){re.escape(token)}(?![가-힣a-z0-9])", " ", text)
    text = PUNCT_RE.sub("", text)
    return text.strip("_")


def add_normalized_name_column(df, source_col: str = "name", target_col: str = "brand_key"):
    """Return a copy with a normalized brand-key column."""

    result = df.copy()
    result[target_col] = result[source_col].map(normalize_brand_name)
    return result


def best_name(*values: Any) -> str:
    """Pick the first non-empty display name."""

    for value in values:
        if not _is_missing(value):
            text = str(value).strip()
            if text and text.lower() not in {"nan", "none", "null"}:
                return text
    return ""
