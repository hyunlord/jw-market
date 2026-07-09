from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from pipeline.scripts.api.brand_activity_csd_shared import BrandChoice


UBIST_FACTOR_KEYS = ("seller", "molecule_strength", "form", "route", "reimbursement")
IQVIA_FACTOR_KEYS = ("mfr_name_kor", "molecule_type", "molecule_desc", "pack_desc", "strength", "nhi_type")


def empty_factor_values(keys: Sequence[str]) -> dict[str, list[str]]:
    return {key: [] for key in keys}


def fallback_brand_choices(brand_key: str, brand_name: str) -> tuple[BrandChoice, ...]:
    return (BrandChoice(brand_key=brand_key, brand_name=brand_name, sales_rank=None, is_selected=True),)


def build_brand_factors(
    choices: Sequence[BrandChoice],
    *,
    selected_brand_key: str,
    cached_elements_by_key: Mapping[str, Mapping[str, Any]],
    selected_factors: Mapping[str, Any],
    strength_by_source_by_key: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Group factors and source-level strength into source-scoped brand slots."""

    items: list[dict[str, Any]] = []
    for index, choice in enumerate(choices, start=1):
        is_selected = choice.is_selected or choice.brand_key == selected_brand_key
        cached = cached_elements_by_key.get(choice.brand_key, {})
        cached_factors = _dict_or_empty(cached.get("factors"))
        factors = cached_factors or (dict(selected_factors) if is_selected else {})
        strength_by_source = _dict_or_empty((strength_by_source_by_key or {}).get(choice.brand_key))
        items.append(
            {
                "brand": choice.brand_name,
                "brand_key": choice.brand_key,
                "role": "selected" if is_selected else "competitor",
                "rank": index,
                "iqvia": _source_section(factors.get("iqvia"), strength_by_source.get("iqvia"), IQVIA_FACTOR_KEYS),
                "ubist": _source_section(factors.get("ubist"), strength_by_source.get("ubist"), UBIST_FACTOR_KEYS),
            }
        )
    return items


def _source_section(factors: Any, strength: Any, factor_keys: Sequence[str]) -> dict[str, Any]:
    factor_section = _source_factor_section(factors, factor_keys)
    strength_section = _source_strength_section(strength)
    if not factor_section["available"] and not strength_section:
        return {}
    return {"factors": factor_section, "strength": strength_section}


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
