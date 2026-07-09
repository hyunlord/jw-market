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


def build_brand_elements(
    choices: Sequence[BrandChoice],
    *,
    selected_brand_key: str,
    cached_elements_by_key: Mapping[str, Mapping[str, Any]],
    selected_factors: Mapping[str, Any],
    selected_strength: Mapping[str, Any],
    strength_by_source_by_key: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Combine factors and strength into one six-slot response contract."""

    items: list[dict[str, Any]] = []
    for index, choice in enumerate(choices, start=1):
        is_selected = choice.is_selected or choice.brand_key == selected_brand_key
        cached = cached_elements_by_key.get(choice.brand_key, {})
        cached_factors = _dict_or_empty(cached.get("factors"))
        cached_strength = _dict_or_empty(cached.get("strength"))
        factors = cached_factors or (dict(selected_factors) if is_selected else {})
        strength = cached_strength or (dict(selected_strength) if is_selected else _unavailable("not_generated"))
        strength_by_source = _dict_or_empty((strength_by_source_by_key or {}).get(choice.brand_key))
        items.append(
            {
                "brand": choice.brand_name,
                "brand_key": choice.brand_key,
                "role": "selected" if is_selected else "competitor",
                "rank": index,
                "sales_rank": choice.sales_rank,
                "factors": {
                    "atc": _string_list(factors.get("atc")),
                    "ubist": _source_factor_section(factors.get("ubist"), UBIST_FACTOR_KEYS),
                    "iqvia": _source_factor_section(factors.get("iqvia"), IQVIA_FACTOR_KEYS),
                },
                "strength": _normalize_strength(strength),
                "strength_by_source": strength_by_source,
            }
        )
    return items


def _source_factor_section(value: Any, keys: Sequence[str]) -> dict[str, Any]:
    values = _factor_values(value, keys)
    available = any(values[key] for key in keys)
    section: dict[str, Any] = {"available": available, "values": values}
    if not available:
        section["reason"] = "not_generated"
    return section


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


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason}


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


def _normalize_strength(value: Mapping[str, Any]) -> dict[str, Any]:
    strength = dict(value) if isinstance(value, Mapping) else {}
    if not strength:
        return _unavailable("not_generated")
    if "available" not in strength:
        strength["available"] = True
    return strength
