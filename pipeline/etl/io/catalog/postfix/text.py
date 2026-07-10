from __future__ import annotations

import re
import unicodedata
from typing import Any

import pandas as pd

from pipeline.etl.io.mart.brand_key_normalize import normalize_brand_name


BASE_DOSAGE_TOKENS = [
    "에스알 캡슐", "연질캡슐", "서방정", "캡슐", "캅셀", "정제", "정", "시럽",
    "주사제", "주사", "주", "패취", "패치", "연고", "액제", "액상", "액",
    "과립", "산제", "점안", "XR",
]
COMBO_STRENGTH_RE = re.compile(r"\s*\d+(\.\d+)?\s*/\s*\d+(\.\d+)?\s*(mg|mcg|μg|g|mL|ml|L|l|IU|U)?\b", re.IGNORECASE)
SINGLE_STRENGTH_RE = re.compile(r"\s*\d+(\.\d+)?\s*(mg|mcg|μg|g|mL|ml|L|l|IU|U|%|개|정|회분)\b", re.IGNORECASE)
BRACKET_RE = re.compile(r"\[[^\]]+\]|\([^)]*\)")


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def extract_brand_base_name(raw_name: Any) -> str:
    """Return display-level brand name by removing SKU dosage/strength noise."""
    if _is_missing(raw_name):
        return ""
    text = unicodedata.normalize("NFKC", str(raw_name)).strip()
    if text.lower() in {"", "nan", "none", "null"}:
        return ""
    text = BRACKET_RE.sub(" ", text)
    text = COMBO_STRENGTH_RE.sub(" ", text)
    text = SINGLE_STRENGTH_RE.sub(" ", text)
    for token in sorted(BASE_DOSAGE_TOKENS, key=len, reverse=True):
        escaped = re.escape(token)
        text = re.sub(rf"\s*{escaped}\s*$", " ", text, flags=re.IGNORECASE)
        text = re.sub(rf"\s*{escaped}\s+", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()
