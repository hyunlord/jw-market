from __future__ import annotations

import json
from typing import Any, Mapping

from pipeline.scripts.api.brand_activity_csd_shared import JsonMap, float_value, json_map, text
from pipeline.scripts.api.dynamic_market.channel_axis import (
    ChannelAxisFilter,
    ChannelAxisPair,
    history_from_channel_specialty_matrix,
    parse_channel_specialty_matrix,
    slice_channel_specialty_matrix,
)
from pipeline.scripts.utils.ubist_channel_mapping import parse_channel_code, raw_pair_to_channel_code


def parse_ubist_channel_axis(value: Mapping[str, Any] | None) -> ChannelAxisFilter | None:
    """Parse the shared filter-options channel_axis contract for Brand Activity APIs."""

    if not isinstance(value, Mapping):
        return None
    raw_axis = value.get("channel_axis")
    if not isinstance(raw_axis, Mapping):
        return None
    ubist = raw_axis.get("ubist", raw_axis)
    if not isinstance(ubist, Mapping):
        return None
    pairs = tuple(
        ChannelAxisPair(facility=text(item.get("facility")).strip(), specialty=text(item.get("specialty")).strip())
        for item in _items(ubist.get("pairs"))
        if text(item.get("facility")).strip() and text(item.get("specialty")).strip()
    )
    selected = ChannelAxisFilter(
        source="ubist",
        facilities=_strings(ubist.get("facility")),
        specialties=_strings(ubist.get("specialty")),
        pairs=pairs,
    )
    return selected if selected.is_active else None


def channel_axis_echo(channel_axis: ChannelAxisFilter | None) -> JsonMap:
    """Return the public echo payload for an applied UBIST channel-axis filter."""

    if channel_axis is None or not channel_axis.is_active:
        return {}
    return {
        "source": channel_axis.source,
        "facility": list(channel_axis.facilities),
        "specialty": list(channel_axis.specialties),
        "pairs": [
            {"facility": item.facility, "specialty": item.specialty}
            for item in channel_axis.pairs
        ],
    }


def channel_axis_sales_value(row: Mapping[str, Any] | None, channel_axis: ChannelAxisFilter | None, quarter: str) -> float:
    """Return sales for one UBIST brand row under the selected channel axis."""

    if row is None or channel_axis is None or not channel_axis.is_active:
        return 0.0
    history = _channel_axis_history(row, channel_axis)
    if not history:
        return 0.0
    if quarter in history:
        return float_value(history.get(quarter))
    prefix = quarter.replace("-Q1", "-0").replace("-Q2", "-0").replace("-Q3", "-0").replace("-Q4", "-")
    if "-Q" in quarter:
        year, qtext = quarter.split("-Q", 1)
        months_by_q = {"1": ("01", "02", "03"), "2": ("04", "05", "06"), "3": ("07", "08", "09"), "4": ("10", "11", "12")}
        return sum(float_value(history.get(f"{year}-{month}")) for month in months_by_q.get(qtext, ()))
    return sum(float_value(value) for value in history.values())


def _channel_axis_history(row: Mapping[str, Any], channel_axis: ChannelAxisFilter) -> dict[str, float]:
    matrix = parse_channel_specialty_matrix(row.get("channel_specialty_matrix"))
    if matrix:
        return history_from_channel_specialty_matrix(slice_channel_specialty_matrix(matrix, channel_axis))

    code_series = _channel_series(row.get("ubist_channel_by_code"))
    if code_series:
        return _history_from_channel_codes(code_series, channel_axis)

    display_series = _channel_series(row.get("ubist_channel_by_display"))
    if display_series:
        return _history_from_channel_displays(display_series, channel_axis)

    return _history_from_axis_series(row, channel_axis)


def _history_from_channel_codes(series_by_code: Mapping[str, Mapping[str, float]], channel_axis: ChannelAxisFilter) -> dict[str, float]:
    history: dict[str, float] = {}
    for code, series in series_by_code.items():
        try:
            parsed = parse_channel_code(code)
        except ValueError:
            parsed = None
        if parsed is None or not _parsed_channel_selected(parsed.facility_raw_values, parsed.specialty_raw_values, channel_axis):
            continue
        _add_series(history, series)
    return history


def _history_from_channel_displays(series_by_display: Mapping[str, Mapping[str, float]], channel_axis: ChannelAxisFilter) -> dict[str, float]:
    history: dict[str, float] = {}
    selected_displays = _selected_display_names(channel_axis)
    for display, series in series_by_display.items():
        if selected_displays and display not in selected_displays:
            continue
        _add_series(history, series)
    return history


def _history_from_axis_series(row: Mapping[str, Any], channel_axis: ChannelAxisFilter) -> dict[str, float]:
    history: dict[str, float] = {}
    if channel_axis.facilities:
        facility_series = _channel_series(row.get("channel_data"))
        for facility in channel_axis.facilities:
            _add_series(history, facility_series.get(facility, {}))
    if channel_axis.specialties:
        specialty_series = _channel_series(row.get("specialty_data"))
        for specialty in channel_axis.specialties:
            _add_series(history, specialty_series.get(specialty, {}))
    return history


def _parsed_channel_selected(facilities: tuple[str, ...], specialties: tuple[str, ...], channel_axis: ChannelAxisFilter) -> bool:
    if channel_axis.pairs:
        pair_codes = {
            raw_pair_to_channel_code(item.facility, item.specialty)
            for item in channel_axis.pairs
        }
        selected_codes = {
            raw_pair_to_channel_code(facility, specialty)
            for facility in facilities
            for specialty in specialties
        }
        return bool(pair_codes & selected_codes)
    if channel_axis.facilities and not (set(channel_axis.facilities) & set(facilities)):
        return False
    if channel_axis.specialties and not (set(channel_axis.specialties) & set(specialties)):
        return False
    return True


def _selected_display_names(channel_axis: ChannelAxisFilter) -> set[str]:
    displays: set[str] = set()
    for item in channel_axis.pairs:
        code = raw_pair_to_channel_code(item.facility, item.specialty)
        if code:
            try:
                parsed = parse_channel_code(code)
            except ValueError:
                parsed = None
            if parsed:
                displays.add(parsed.display_name)
    return displays


def _channel_series(raw: Any) -> dict[str, dict[str, float]]:
    if isinstance(raw, Mapping):
        payload = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    else:
        return {}
    return {
        str(key): {str(period): float_value(value) for period, value in json_map(series).items()}
        for key, series in payload.items()
        if json_map(series)
    }


def _add_series(target: dict[str, float], series: Mapping[str, float]) -> None:
    for period, value in series.items():
        target[str(period)] = target.get(str(period), 0.0) + float_value(value)


def _items(value: Any) -> tuple[Mapping[str, Any], ...]:
    return tuple(item for item in value if isinstance(item, Mapping)) if isinstance(value, list | tuple) else ()


def _strings(value: Any) -> tuple[str, ...]:
    raw_values = value if isinstance(value, list | tuple) else [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        candidate = text(item).strip()
        if candidate and candidate not in seen:
            result.append(candidate)
            seen.add(candidate)
    return tuple(result)
