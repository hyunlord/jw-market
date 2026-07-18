from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from jw_chat_agent_poc.service.chart_utils import PALETTE, as_mapping, number, series_from_list, series_from_mapping, text


def market_size_series(render_data: Mapping[str, Any]) -> tuple[list[str], list[float]] | None:
    return (
        series_from_mapping(render_data.get("series"))
        or series_from_list(render_data.get("market_size_series"), value_key="value_krw")
        or series_from_list(render_data.get("market_size_series"), value_key="value")
    )


def top_brand_share_series(render_data: Mapping[str, Any]) -> tuple[list[str], list[dict[str, Any]]] | None:
    trends = render_data.get("level_top5_trend_series")
    if not isinstance(trends, Sequence) or isinstance(trends, (str, bytes)):
        return None
    labels: list[str] | None = None
    datasets: list[dict[str, Any]] = []
    for index, item in enumerate(trends[:5]):
        row = as_mapping(item)
        if not row:
            continue
        brand = text(row.get("brand"))
        raw_series = row.get("series")
        if not brand or not isinstance(raw_series, Sequence) or isinstance(raw_series, (str, bytes)):
            continue
        row_labels: list[str] = []
        values: list[float | None] = []
        for point in raw_series:
            point_map = as_mapping(point)
            if not point_map:
                continue
            label = text(point_map.get("period"))
            value = number(point_map.get("ms_pct"))
            if label is None:
                continue
            row_labels.append(label)
            values.append(value)
        if len(row_labels) < 2 or not any(value is not None for value in values):
            continue
        if labels is None:
            labels = row_labels
        if row_labels != labels:
            continue
        datasets.append({"label": f"{brand} MS", "data": values, "borderColor": PALETTE[index % len(PALETTE)], "unit": "%"})
    if labels is None or len(datasets) < 2:
        return None
    return labels, datasets
