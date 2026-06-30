"""Channel fallback helpers for strategic dynamic-market overlays."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any


JsonRow = dict[str, Any]


def runtime_resolve_market_channels(original_resolver: Any) -> Any:
    """Wrap the cause channel resolver with a mart ``specialty_data`` fallback."""

    def resolve(*, rows: list[JsonRow], market: JsonRow | None, measure: str, max_channels: int = 4) -> JsonRow:
        resolved = original_resolver(rows=rows, market=market, measure=measure, max_channels=max_channels)
        specialty_channels = resolved.get("specialty_channels") if isinstance(resolved, dict) else None
        if isinstance(specialty_channels, list) and len(specialty_channels) > 1:
            return resolved
        fallback = _specialty_channels_from_mart_rows(rows, max_channels=max_channels)
        return fallback or resolved

    return resolve


def _specialty_channels_from_mart_rows(rows: list[JsonRow], *, max_channels: int) -> JsonRow | None:
    totals: dict[str, float] = {}
    per_row_series: list[tuple[JsonRow, dict[str, Any]]] = []
    for row in rows:
        specialty_data = _decode_object(row.get("specialty_data"))
        if not specialty_data:
            continue
        per_row_series.append((row, specialty_data))
        for channel, series in specialty_data.items():
            channel_text = str(channel).strip()
            if not channel_text or channel_text == "전체" or channel_text.lower().startswith("others("):
                continue
            if isinstance(series, dict):
                totals[channel_text] = totals.get(channel_text, 0.0) + sum(
                    _history_item_value(item) for item in series.values()
                )
    selected = [channel for channel, _ in sorted(totals.items(), key=lambda item: item[1], reverse=True)[:max_channels]]
    if not selected:
        return None
    for row, specialty_data in per_row_series:
        selected_series = {channel: specialty_data.get(channel, {}) for channel in selected}
        row["__ubist_dual_channel_data"] = selected_series
        row["__ubist_specialty_channel_data"] = selected_series
    target_channels = [{"code": channel, "display_name": channel} for channel in selected]
    return {
        "channels": ["전체", "상급종병", "종병", "병원", "의원", "보건소", "기타"],
        "specialty_channels": ["전체", *selected],
        "target_channels": target_channels,
        "specialty_target_channels": target_channels,
        "fallback_codes": selected,
        "series_brand_count": len(per_row_series),
        "raw_brand_count": len(rows),
        "fallback_source": "mart_specialty_data",
    }


def _decode_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _history_item_value(item: Any) -> float:
    raw = item.get("raw_value", item.get("value", 0.0)) if isinstance(item, Mapping) else item
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0
