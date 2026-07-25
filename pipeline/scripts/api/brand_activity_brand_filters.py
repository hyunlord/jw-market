from __future__ import annotations

from typing import Any, Final, Mapping

from pipeline.contracts.dimension_registry import api_dimension_names, normalize_dimension_value
from pipeline.domain.molecules import split_molecule_components
from pipeline.scripts.api.brand_activity_channel_axis import audit_code_axis_echo, parse_audit_code_axis
from pipeline.scripts.api.brand_activity_csd_shared import JsonMap, text


FILTER_DIMENSIONS_BY_VIEW: Final[dict[str, tuple[str, ...]]] = {
    "general": ("atc4", "molecule", "mfr", "molecule_type", "molecule_desc", "pack", "strength", "nhi"),
    "strategic_ml": ("atc4", "molecule", "class"),
    "strategic_cd": ("atc4", "molecule", "class"),
}
IQVIA_ANALYSIS_LEVEL_DIMENSIONS: Final = api_dimension_names("iqvia_nsa")


def applied_brand_filter(view_name: str, market_id: str, filter_payload: Mapping[str, Any]) -> JsonMap:
    """Return normalized filter values for the dimensions supported by one view."""
    allowed = FILTER_DIMENSIONS_BY_VIEW[view_name]
    applied: JsonMap = {}
    for dimension in allowed:
        values = _normalized_filter_values(dimension, filter_payload.get(dimension))
        if values:
            applied[dimension] = list(values)
    if view_name == "general":
        for api_name, dimension in IQVIA_ANALYSIS_LEVEL_DIMENSIONS.items():
            values = _normalized_filter_values(dimension, _iqvia_analysis_level_value(filter_payload, api_name))
            if values:
                applied[dimension] = list(values)
    channel_axis = parse_audit_code_axis(filter_payload) if view_name == "general" else None
    if channel_axis:
        applied["channel_axis"] = audit_code_axis_echo(channel_axis)
    if view_name == "general" and "atc4" not in applied:
        applied["atc4"] = [market_id.strip().upper()]
    return applied


def _normalized_filter_values(dimension: str, value: Any) -> tuple[str, ...]:
    """Normalize one request filter dimension into comparison keys."""
    raw_values = value if isinstance(value, list | tuple) else [value]
    values: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        normalized_items = _normalize_dimension_value(dimension, item)
        for normalized in normalized_items:
            if normalized and normalized not in seen:
                seen.add(normalized)
                values.append(normalized)
    return tuple(values)


def _normalize_dimension_value(dimension: str, value: Any) -> tuple[str, ...]:
    """Normalize one filter scalar according to its registry dimension."""
    raw = text(value).strip()
    if not raw:
        return ()
    if dimension == "atc4":
        return (raw.upper(),)
    if dimension == "molecule":
        return tuple(component.norm for component in split_molecule_components(raw))
    if dimension == "class":
        return (raw,)
    if dimension in {"mfr", "molecule_type", "molecule_desc", "pack", "strength", "nhi"}:
        normalized = normalize_dimension_value(raw)
        return (normalized,) if normalized else ()
    return ()


def _iqvia_analysis_level_value(filter_payload: Mapping[str, Any], api_name: str) -> Any:
    analysis_level = filter_payload.get("analysis_level")
    if not isinstance(analysis_level, Mapping):
        return None
    iqvia = analysis_level.get("iqvia")
    if not isinstance(iqvia, Mapping):
        return None
    return iqvia.get(api_name)
