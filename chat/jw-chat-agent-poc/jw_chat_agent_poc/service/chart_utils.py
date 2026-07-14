from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


TEAL = "#0f9f9a"
SLATE = "#64748b"
ORANGE = "#f97316"
BLUE = "#2563eb"
PURPLE = "#7c3aed"
PALETTE = [TEAL, BLUE, ORANGE, PURPLE, "#16a34a", "#dc2626", "#0891b2", "#ca8a04"]


def share_chart(rows: Sequence[Mapping[str, Any]], target_brand: str | None) -> dict[str, Any]:
    def row_order(row: Mapping[str, Any]) -> tuple[float, float]:
        rank = number(row.get("rank"))
        share = number(row.get("ms_recent_pct"))
        return (rank if rank is not None else 9999.0, -(share if share is not None else float("-inf")))

    top_rows = sorted(rows, key=row_order)[:8]
    labels = [text(row.get("brand")) or text(row.get("name")) or "미분류" for row in top_rows]
    values = [number(row.get("ms_recent_pct")) for row in top_rows]
    colors = [TEAL if target_brand and label == target_brand else SLATE for label in labels]
    return {
        "type": "bar",
        "title": "점유율 상위 브랜드",
        "labels": labels,
        "datasets": [{"label": "M/S %", "data": values, "backgroundColor": colors, "unit": "%"}],
        "source": "cache market share",
        "unit": "%",
    }


def bar_chart(title: str, values_by_label: Mapping[str, float | None], source: str, unit: str) -> dict[str, Any]:
    labels = list(values_by_label.keys())
    values = [values_by_label[label] for label in labels]
    return {
        "type": "bar",
        "title": title,
        "labels": labels,
        "datasets": [{"label": unit, "data": values, "backgroundColor": PALETTE[: len(labels)], "unit": unit}],
        "source": source,
        "unit": unit,
    }


def doughnut_chart(title: str, values_by_label: Mapping[str, float], source: str, unit: str) -> dict[str, Any]:
    labels = list(values_by_label.keys())
    values = [values_by_label[label] for label in labels]
    return {
        "type": "doughnut",
        "title": title,
        "labels": labels,
        "datasets": [{"label": unit, "data": values, "backgroundColor": PALETTE[: len(labels)], "unit": unit}],
        "source": source,
        "unit": unit,
    }


def line_chart(
    title: str,
    labels: Sequence[str],
    datasets: Sequence[Mapping[str, Any]],
    source: str,
    unit: str,
) -> dict[str, Any]:
    return {
        "type": "line",
        "title": title,
        "labels": list(labels),
        "datasets": [dict(dataset) for dataset in datasets],
        "source": source,
        "unit": unit,
    }


def series_from_mapping(value: Any) -> tuple[list[str], list[float | None]] | None:
    mapping = as_mapping(value)
    if not mapping:
        return None
    labels = sorted(str(key) for key in mapping.keys())
    values: list[float | None] = []
    for label in labels:
        item = mapping.get(label)
        item_value = (as_mapping(item) or {}).get("value") if isinstance(item, Mapping) else item
        item_number = number(item_value)
        values.append(item_number)
    return labels, values


def series_from_list(value: Any, value_key: str) -> tuple[list[str], list[float | None]] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    labels: list[str] = []
    values: list[float | None] = []
    for item in value:
        row = as_mapping(item)
        if not row:
            return None
        label = text(row.get("period")) or text(row.get("year"))
        item_number = number(row.get(value_key))
        if label is None:
            return None
        labels.append(label)
        values.append(item_number)
    return labels, values


def hhi_series(value: Any) -> tuple[list[str], list[float]] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    labels: list[str] = []
    values: list[float] = []
    for item in value:
        row = as_mapping(item)
        if not row:
            return None
        label = text(row.get("period")) or text(row.get("year"))
        item_number = number(row.get("hhi"))
        if label is None or item_number is None:
            return None
        labels.append(label)
        values.append(item_number)
    return labels, values


def brand_rows(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    trend = as_mapping(data.get("level_top5_trend")) or {}
    by_level = as_mapping(trend.get("by_level")) or {}
    brand_level = as_mapping(by_level.get("Brand")) or {}
    values = brand_level.get("values")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or not values:
        return []
    first_value = as_mapping(values[0]) or {}
    rows = first_value.get("brands_in_value")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def dedupe_charts(charts: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for chart in charts:
        title = str(chart.get("title", ""))
        if not title or title in seen:
            continue
        seen.add(title)
        unique.append(dict(chart))
    return unique


def as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def text(value: Any) -> str | None:
    if value is None:
        return None
    value_text = str(value).strip()
    return value_text or None


def number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def has_filtered_metric_call(calls: Sequence[Mapping[str, Any]]) -> bool:
    for call in calls:
        render_data = as_mapping(call.get("render_data")) or {}
        applied = render_data.get("applied_filters")
        unsupported = render_data.get("unsupported_filters")
        if isinstance(applied, Mapping) and applied:
            return True
        if isinstance(unsupported, list) and unsupported:
            return True
    return False
