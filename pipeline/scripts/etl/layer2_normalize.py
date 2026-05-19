#!/usr/bin/env python3
"""Normalization helpers for Layer 2 enriched fact generation."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops_utils import find_project_root  # noqa: E402


REPO_ROOT = find_project_root(Path(__file__).resolve())


CATALOG_TO_UBIST_ATC_MAP = {
    "A02B2": "A2B2",
    "A06B2": "A6B2",
    "C01D0": "C1D",
    "G04C0": "G4C0",
}


def clean_scalar(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def normalize_atc(atc_code: Any) -> str:
    """Normalize catalog/IQVIA ATC code to the closest UBIST bracket-code form."""
    text = clean_scalar(atc_code).upper()
    if not text:
        return ""
    if text in CATALOG_TO_UBIST_ATC_MAP:
        return CATALOG_TO_UBIST_ATC_MAP[text]
    return text


def extract_bracket_code(value: Any) -> str:
    """Extract first bracket code from values such as '[C10A1] ...' or '... [111501ATB]'."""
    text = clean_scalar(value)
    match = re.search(r"\[([^\]]+)\]", text)
    return match.group(1).strip() if match else ""


def normalize_product_title(value: Any) -> str:
    """Normalize full product title while preserving dose tokens.

    This is intentionally stricter than brand normalization. Phase 16-D-PRE showed
    UBIST product identity is reliable when the strategic product title and UBIST
    `제품` are compared after whitespace/unit normalization.
    """
    text = clean_scalar(value).lower()
    text = text.replace("㎎", "mg").replace("ＭＧ", "mg")
    text = text.replace("μg", "mcg").replace("㎍", "mcg")
    text = re.sub(r"\s+", "", text)
    return text


def normalize_brand(value: Any) -> str:
    """Normalize product/brand text for fallback matching."""
    text = clean_scalar(value).lower()
    text = text.replace("㎎", "mg").replace("ＭＧ", "mg")
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(
        r"\b\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?\s*"
        r"(?:mg|g|ml|iu|mcg)?\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:mg|g|ml|iu|mcg)\b", " ", text, flags=re.IGNORECASE)
    for token in [
        "필름코팅정",
        "연질캡슐",
        "장용정",
        "서방정",
        "복합정",
        "프리필드펜",
        "캡슐",
        "정",
        "주사",
        "주",
        "액",
        "시럽",
    ]:
        text = text.replace(token, " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_strength(value: Any) -> str:
    text = clean_scalar(value).lower()
    text = text.replace("㎎", "mg").replace("ＭＧ", "mg")
    text = text.replace("μg", "mcg").replace("㎍", "mcg")
    text = re.sub(r"\s+", "", text)
    return text


def load_customer_dictionary(path: Path | None = None) -> dict[str, Any]:
    catalog_path = path or (REPO_ROOT / "catalog" / "customer_dictionary.yaml")
    with catalog_path.open(encoding="utf-8") as fp:
        return yaml.safe_load(fp) or {}


def map_channel_ubist(value: Any, customer_dict: dict[str, Any] | None = None) -> str:
    mapping = (customer_dict or load_customer_dictionary()).get("ubist_channel", {})
    text = clean_scalar(value)
    return mapping.get(text, "Unknown" if text else "")


def map_specialty_ubist(value: Any, customer_dict: dict[str, Any] | None = None) -> str:
    mapping = (customer_dict or load_customer_dictionary()).get("ubist_specialty", {})
    text = clean_scalar(value)
    if text in mapping:
        return mapping[text]
    for raw, canonical in mapping.items():
        if raw and raw in text:
            return canonical
    return "Unknown" if text else ""


def canonical_iqvia_channel(audit_code: Any) -> str:
    text = clean_scalar(audit_code).upper()
    for prefix in ("KHPA", "KCPA", "KPA"):
        if text.startswith(prefix) or prefix in text:
            return prefix
    return text or "Unknown"
