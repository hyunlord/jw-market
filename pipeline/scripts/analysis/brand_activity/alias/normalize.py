from __future__ import annotations

import re
import unicodedata
from typing import Final


# PL-approved punctuation variants only; suffix and look-alike products stay separate.
VARIANT_RULES: Final[dict[str, tuple[str, ...]]] = {
    "APITO": ("A-PITO", "APITO"),
    "LOW OSMO PERI": ("LOW OSMO PERI", "LOWOSMOPERI"),
}
VARIANT_TO_ANCHOR: Final[dict[str, str]] = {
    variant: anchor
    for anchor, variants in VARIANT_RULES.items()
    for variant in variants
}


def clean_product_name(value: str) -> str:
    text = unicodedata.normalize("NFC", value or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text.upper()


def normalize_iqvia_en(value: str) -> str:
    cleaned = clean_product_name(value)
    return VARIANT_TO_ANCHOR.get(cleaned, cleaned)


def configured_variants_for(anchor: str) -> tuple[str, ...]:
    return VARIANT_RULES.get(anchor, (anchor,))


def configured_variant_rule_count() -> int:
    return len(VARIANT_RULES)
