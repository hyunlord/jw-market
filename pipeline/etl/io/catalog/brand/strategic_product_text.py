from __future__ import annotations

import re
import unicodedata
from typing import Any

from pipeline.etl.io.mart.brand_alias_resolver import (
    MANUAL_BRAND_ALIASES,
    BrandAliasResolver,
)


_BRAND_ALIAS_RESOLVER = BrandAliasResolver.from_static(MANUAL_BRAND_ALIASES.items())


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value)).strip()
    if not text or text.lower() == "nan":
        return None
    if text in {"#N/A", "N/A", "NA"}:
        return None
    return _BRAND_ALIAS_RESOLVER.resolve_alias(text)


def normalize_key(value: Any) -> str:
    text = clean_text(value) or ""
    text = re.sub(r"\s+", "", text)
    return text.upper().replace("_", "-")


def manufacturer_key(value: Any) -> str:
    key = normalize_key(value)
    aliases = {
        normalize_key("제이더블유중외제약"): normalize_key("JW중외제약"),
        normalize_key("제이더블유생명과학"): normalize_key("JW생명과학"),
    }
    return aliases.get(key, key)


def source_row_id_from_brand_id(brand_id: str) -> int:
    return int(brand_id.rsplit("_", 1)[1])


def ml_index_from_brand_id(brand_id: str) -> int:
    return int(brand_id.split("_")[1])

def extract_atc_code(value: Any) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    bracket = re.search(r"\[([A-Z0-9]+)\]", text.upper())
    if bracket:
        return bracket.group(1)
    plain = re.search(r"\b([A-Z][0-9][A-Z0-9]{2,3})\b", text.upper())
    return plain.group(1) if plain else text


def make_product_name(product_name: Any, pack_or_strength: Any) -> str | None:
    product = clean_text(product_name)
    pack = clean_text(pack_or_strength)
    if product is None:
        return None
    if pack is None:
        return product
    if normalize_key(pack) in normalize_key(product):
        return product
    return f"{product} {pack}"


def sheet_product_name(brand_row: dict[str, Any]) -> str:
    """Use the sheet row as product grain when it already carries pack/strength."""
    name = str(brand_row["name"])
    strength = clean_text(brand_row.get("strength_pack"))
    if strength is None:
        return name
    if normalize_key(strength) in normalize_key(name):
        return name
    return f"{name} {strength}"


def is_sheet_product_grain(brand_row: dict[str, Any], context: dict[str, Any] | None = None) -> bool:
    """Rows with an explicit strength/pack are already product-grain in MI Master."""
    if context and context.get("strategic_market_id") == "strategy_007":
        # strategy_007 strength_pack is Phase 14 serving materialization from 성분용량;
        # keep UBIST product expansion semantics from Step 14-6.
        return False
    return clean_text(brand_row.get("strength_pack")) is not None

def source_order_for_data_source(data_source: str) -> tuple[str, ...]:
    if data_source == "ubist":
        return ("UBIST",)
    if data_source == "iqvia":
        return ("IQVIA",)
    if data_source == "both":
        return ("UBIST", "IQVIA")
    raise ValueError(f"unknown data_source: {data_source}")
