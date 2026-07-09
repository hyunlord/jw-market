from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from pipeline.scripts.api.brand_activity_csd_shared import BrandChoice


UBIST_FACTOR_KEYS = ("seller", "molecule_strength", "form", "route", "reimbursement")
IQVIA_FACTOR_KEYS = ("mfr_name_kor", "molecule_type", "molecule_desc", "pack_desc", "strength", "nhi_type")


def empty_factor_values(keys: Sequence[str]) -> dict[str, list[str]]:
    return {key: [] for key in keys}


def fallback_brand_choices(brand_key: str, brand_name: str) -> tuple[BrandChoice, ...]:
    return (BrandChoice(brand_key=brand_key, brand_name=brand_name, sales_rank=None, is_selected=True),)


def build_brand_factor_items(
    choices: Sequence[BrandChoice],
    *,
    selected_brand_key: str,
    selected_factors: Mapping[str, Any],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, choice in enumerate(choices, start=1):
        is_selected = choice.is_selected or choice.brand_key == selected_brand_key
        factors = selected_factors if is_selected else {}
        items.append(
            {
                "brand": choice.brand_name,
                "brand_key": choice.brand_key,
                "role": "selected" if is_selected else "competitor",
                "rank": index,
                "sales_rank": choice.sales_rank,
                "atc": _string_list(factors.get("atc")),
                "ubist": _source_factor_section(factors.get("ubist"), UBIST_FACTOR_KEYS),
                "iqvia": _source_factor_section(factors.get("iqvia"), IQVIA_FACTOR_KEYS),
            }
        )
    return items


def build_brand_strength_items(
    choices: Sequence[BrandChoice],
    *,
    selected_brand_key: str,
    selected_strength: Mapping[str, Any],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, choice in enumerate(choices, start=1):
        is_selected = choice.is_selected or choice.brand_key == selected_brand_key
        overall = deepcopy(dict(selected_strength)) if is_selected else _unavailable("not_generated")
        available = bool(overall.get("available")) if isinstance(overall, dict) else False
        items.append(
            {
                "brand": choice.brand_name,
                "brand_key": choice.brand_key,
                "role": "selected" if is_selected else "competitor",
                "rank": index,
                "sales_rank": choice.sales_rank,
                "available": available,
                "overall": overall if isinstance(overall, dict) else _unavailable("not_generated"),
                "ubist": _unavailable("source_strength_not_generated"),
                "iqvia": _unavailable("source_strength_not_generated"),
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
