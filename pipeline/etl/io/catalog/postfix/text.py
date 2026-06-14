from __future__ import annotations

import re
import unicodedata
from typing import Any

import pandas as pd

DOSAGE_TOKENS = [
    "정", "정제", "캡슐", "캡슐제", "연질캡슐", "소프트캡슐", "주", "주사",
    "주사제", "시럽", "현탁액", "액", "크림", "겔", "연고", "패취", "패치",
    "tab", "tabs", "tablet", "tablets", "cap", "caps", "capsule", "capsules",
    "inj", "injection", "syr", "syrup",
]
BASE_DOSAGE_TOKENS = [
    "에스알 캡슐", "연질캡슐", "서방정", "캡슐", "캅셀", "정제", "정", "시럽",
    "주사제", "주사", "주", "패취", "패치", "연고", "액제", "액상", "액",
    "과립", "산제", "점안", "XR",
]
UNIT_RE = re.compile(r"(\d+(\.\d+)?)\s*(mg|mcg|g|kg|ml|l|iu|%)", re.IGNORECASE)
COMBO_STRENGTH_RE = re.compile(r"\s*\d+(\.\d+)?\s*/\s*\d+(\.\d+)?\s*(mg|mcg|μg|g|mL|ml|L|l|IU|U)?\b", re.IGNORECASE)
SINGLE_STRENGTH_RE = re.compile(r"\s*\d+(\.\d+)?\s*(mg|mcg|μg|g|mL|ml|L|l|IU|U|%|개|정|회분)\b", re.IGNORECASE)
BRACKET_RE = re.compile(r"\[[^\]]+\]|\([^)]*\)")
PUNCT_RE = re.compile(r"[^\w가-힣]+", re.UNICODE)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def normalize_brand_name(raw_name: Any) -> str:
    """Return the archive brand key used by layer0 postfix joins."""
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
