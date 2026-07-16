"""Deterministic period-window projection for nested mart payloads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import lru_cache
import json
import re
from typing import Any, Final

from pipeline.scripts.api.dynamic_market.types import PeriodRange


_PERIOD_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<year>\d{4})(?:-(?:(?P<month>0[1-9]|1[0-2])|Q(?P<quarter>[1-4])))?$"
)
_ROW_SERIES_FIELDS: Final[tuple[str, ...]] = (
    "raw_value_history",
    "metric_history",
    "extended_metric_history",
    "dimension_data",
    "dimension_channel_data",
    "dimension_specialty_data",
    "channel_data",
    "channel_specialty_matrix",
    "audit_code_matrix",
    "market_size_series",
    "brand_ranking_stacked",
    "company_ranking_stacked",
    "hhi_series_5y",
    "hhi_series",
    "level_top5_trend",
)


def trim_period_rows(rows: Sequence[Mapping[str, Any]], period_range: PeriodRange) -> list[dict[str, Any]]:
    """Copy mart rows while projecting known time-series fields to the requested window."""

    copied = [dict(row) for row in rows]
    if period_range.start is None and period_range.end is None:
        return copied
    for row in copied:
        for field in _ROW_SERIES_FIELDS:
            if field not in row or row[field] in (None, ""):
                continue
            row[field] = _trim_encoded_value(row[field], period_range)
        row.pop("__metric_history", None)
        row.pop("__extended_metric_history", None)
    return copied


def trim_period_payload(value: Any, period_range: PeriodRange) -> Any:
    """Project every nested period-keyed mapping and period-point list without inventing values."""

    if isinstance(value, Mapping):
        items = list(value.items())
        period_items = [(_period_interval(str(key)), key, item) for key, item in items]
        if items and all(interval is not None for interval, _key, _item in period_items):
            return {
                str(key): trim_period_payload(item, period_range)
                for interval, key, item in period_items
                if interval is not None and _overlaps(interval, period_range)
            }
        return {str(key): trim_period_payload(item, period_range) for key, item in items}
    if isinstance(value, list):
        if value and all(isinstance(item, Mapping) and _point_period(item) is not None for item in value):
            return [
                trim_period_payload(item, period_range)
                for item in value
                if (period := _point_period(item)) is not None
                and (interval := _period_interval(period)) is not None
                and _overlaps(interval, period_range)
            ]
        return [trim_period_payload(item, period_range) for item in value]
    return value


def _trim_encoded_value(value: Any, period_range: PeriodRange) -> Any:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        return json.dumps(trim_period_payload(decoded, period_range), ensure_ascii=False, sort_keys=True)
    return trim_period_payload(value, period_range)


def _point_period(item: Mapping[str, Any]) -> str | None:
    value = item.get("period")
    if value is None:
        value = item.get("period_full")
    if value is None:
        value = item.get("year")
    return str(value) if value is not None else None


@lru_cache(maxsize=4096)
def _period_interval(value: str) -> tuple[int, int] | None:
    match = _PERIOD_RE.fullmatch(value)
    if match is None:
        return None
    year = int(match.group("year"))
    if match.group("month") is not None:
        month = int(match.group("month"))
        index = year * 12 + month - 1
        return index, index
    if match.group("quarter") is not None:
        first_month = (int(match.group("quarter")) - 1) * 3
        start = year * 12 + first_month
        return start, start + 2
    return year * 12, year * 12 + 11


def _overlaps(interval: tuple[int, int], period_range: PeriodRange) -> bool:
    start_interval = _period_interval(period_range.start) if period_range.start else None
    end_interval = _period_interval(period_range.end) if period_range.end else None
    start = start_interval[0] if start_interval else None
    end = end_interval[1] if end_interval else None
    return (start is None or interval[1] >= start) and (end is None or interval[0] <= end)
