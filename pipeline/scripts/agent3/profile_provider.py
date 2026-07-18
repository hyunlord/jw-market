from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .json_util import parse_history, parse_json_object


PROFILE_FIELDS = ("class", "molecule", "dosage_form", "strength_pack", "nhi_type", "ox_gx")


@dataclass(frozen=True, slots=True)
class MoleculeRow:
    brand_name: str
    mart_source: str
    molecule_display: str
    component_count: int
    is_combo_component: bool


def build_profile(
    *,
    brand_name: str,
    general_rows: list[dict[str, Any]],
    strategic_rows: list[dict[str, Any]],
    molecule_rows: list[MoleculeRow],
) -> dict[str, Any]:
    """Build the Agent3 brand profile with recoded and raw values side by side."""

    overlay = _merged_overlay(strategic_rows)
    raw_dimensions = _dimension_values(general_rows)
    molecule_values = sorted({row.molecule_display for row in molecule_rows if row.molecule_display})
    max_components = max((row.component_count for row in molecule_rows), default=0)
    latest = _latest_by_source(general_rows)

    profile: dict[str, Any] = {
        "brand": brand_name,
        "sources": sorted({str(row.get("source")) for row in general_rows if row.get("source")}),
        "atc4_codes": sorted({str(row.get("atc4_code")) for row in general_rows if row.get("atc4_code")}),
        "molecule_raw": molecule_values,
        "molecule_components": molecule_values,
        "molecule_component_count": max_components or None,
        "molecule_type": "combination" if max_components > 1 else ("single" if max_components == 1 else None),
        "latest": latest,
    }
    for field in PROFILE_FIELDS:
        profile[f"{field}_recode"] = _blank_to_none(overlay.get(field))
        if field == "molecule" and molecule_values:
            profile[f"{field}_raw"] = molecule_values
        else:
            profile[f"{field}_raw"] = raw_dimensions.get(field, [])
    if not profile["molecule_raw"] and profile["molecule_recode"]:
        profile["molecule_raw"] = [str(profile["molecule_recode"])]
    return profile


def _merged_overlay(rows: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for row in rows:
        overlay = parse_json_object(row.get("overlay_data"))
        for key, value in overlay.items():
            if value not in (None, "", [], {}) and key not in merged:
                merged[key] = value
    return merged


def _dimension_values(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    values: dict[str, set[str]] = {}
    for row in rows:
        dimensions = parse_json_object(row.get("dimension_data"))
        for dimension, members in dimensions.items():
            if not isinstance(members, dict):
                continue
            bucket = values.setdefault(str(dimension), set())
            for member in members:
                if str(member).strip():
                    bucket.add(str(member))
    return {key: sorted(bucket) for key, bucket in values.items()}


def _latest_by_source(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        source = str(row.get("source") or "")
        if not source:
            continue
        history = parse_history(row.get("raw_value_history"))
        if not history:
            continue
        period = max(history)
        current = latest.get(source)
        if current is None or period > str(current.get("period")):
            latest[source] = {
                "period": period,
                "value": history[period],
                "measure": row.get("measure", "sales"),
                "unit_label": row.get("unit_label"),
            }
    return latest


def _blank_to_none(value: Any) -> Any:
    if value in ("", [], {}):
        return None
    return value
