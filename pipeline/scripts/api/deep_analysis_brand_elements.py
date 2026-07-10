from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from pipeline.scripts.api.brand_activity_csd_shared import BrandChoice


UBIST_FACTOR_KEYS = ("seller", "molecule_strength", "form", "route", "reimbursement")
IQVIA_FACTOR_KEYS = ("mfr_name_kor", "molecule_type", "molecule_desc", "pack_desc", "strength", "nhi_type")
FACTOR_KEYS_BY_SOURCE = {"iqvia": IQVIA_FACTOR_KEYS, "ubist": UBIST_FACTOR_KEYS}


def empty_factor_values(keys: Sequence[str]) -> dict[str, list[str]]:
    return {key: [] for key in keys}


def fallback_brand_choices(brand_key: str, brand_name: str) -> tuple[BrandChoice, ...]:
    return (BrandChoice(brand_key=brand_key, brand_name=brand_name, sales_rank=None, is_selected=True),)


def build_brand_factors(
    choices_by_source: Mapping[str, Sequence[BrandChoice]],
    *,
    selected_brand_key: str,
    cached_elements_by_key: Mapping[str, Mapping[str, Any]],
    selected_factors: Mapping[str, Any],
    strength_by_source_by_key: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build independent source-market brand lists with source-local evidence."""

    return {
        source: _build_source_brand_factors(
            source,
            choices_by_source.get(source, ()),
            selected_brand_key=selected_brand_key,
            cached_elements_by_key=cached_elements_by_key,
            selected_factors=selected_factors,
            strength_by_source_by_key=strength_by_source_by_key or {},
        )
        for source in FACTOR_KEYS_BY_SOURCE
    }


def _build_source_brand_factors(
    source: str,
    choices: Sequence[BrandChoice],
    *,
    selected_brand_key: str,
    cached_elements_by_key: Mapping[str, Mapping[str, Any]],
    selected_factors: Mapping[str, Any],
    strength_by_source_by_key: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    factor_keys = FACTOR_KEYS_BY_SOURCE[source]

    items: list[dict[str, Any]] = []
    for choice in choices:
        is_selected = choice.is_selected or choice.brand_key == selected_brand_key
        cached = cached_elements_by_key.get(choice.brand_key, {})
        cached_factors = _dict_or_empty(cached.get("factors"))
        factors = cached_factors or (dict(selected_factors) if is_selected else {})
        source_factors = _source_factor_section(factors.get(source), factor_keys)
        strength_by_source = _dict_or_empty(strength_by_source_by_key.get(choice.brand_key))
        source_strength = _source_strength_section(strength_by_source.get(source))
        if len(choices) == 1 and choice.sales_rank is None and not source_factors["available"] and not source_strength:
            continue
        items.append(
            {
                "brand": choice.brand_name,
                "brand_key": choice.brand_key,
                "role": "selected" if is_selected else "competitor",
                "rank": choice.sales_rank,
                "factors": source_factors,
                "strength": source_strength,
            }
        )
    return items


def _source_factor_section(value: Any, keys: Sequence[str]) -> dict[str, Any]:
    values = _factor_values(value, keys)
    available = any(values[key] for key in keys)
    return {
        "available": available,
        "reason": None if available else "not_generated",
        "values": values,
    }


def _factor_values(value: Any, keys: Sequence[str]) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        return empty_factor_values(keys)
    return {key: _string_list(value.get(key)) for key in keys}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list | tuple):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _dict_or_empty(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _source_strength_section(value: Any) -> dict[str, Any]:
    strength = _dict_or_empty(value)
    if not strength:
        return {}
    strength_items = strength.get("strength_items", [])
    limitations = strength.get("limitations", [])
    return {
        "profile_display": _dict_or_empty(strength.get("profile_display")),
        "strength_items": strength_items if isinstance(strength_items, list) else [],
        "limitations": limitations if isinstance(limitations, list) else [],
    }
