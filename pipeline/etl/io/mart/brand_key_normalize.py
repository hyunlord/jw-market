"""Compatibility exports for brand-name normalization.

New cross-layer callers should import :mod:`pipeline.domain.brand_names`.
"""

from pipeline.domain.brand_names import (
    BASE_DOSAGE_TOKENS,
    BRACKET_RE,
    COMBO_STRENGTH_RE,
    DOSAGE_TOKENS,
    PUNCT_RE,
    SINGLE_STRENGTH_RE,
    UNIT_RE,
    add_normalized_name_column,
    best_name,
    extract_brand_base_name,
    normalize_brand_name,
)


__all__ = (
    "BASE_DOSAGE_TOKENS",
    "BRACKET_RE",
    "COMBO_STRENGTH_RE",
    "DOSAGE_TOKENS",
    "PUNCT_RE",
    "SINGLE_STRENGTH_RE",
    "UNIT_RE",
    "add_normalized_name_column",
    "best_name",
    "extract_brand_base_name",
    "normalize_brand_name",
)
