from __future__ import annotations

from collections.abc import Callable
from typing import Any


def market_size_series(
    value: Any,
    number: Callable[[Any], float | None],
    krw_to_eok: Callable[[Any], float | None],
) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    series: list[dict[str, Any]] = []
    for period in sorted(value):
        item = value.get(period)
        if not isinstance(item, dict):
            continue
        amount = number(item.get("value"))
        series.append(
            {
                "period": period,
                "value_krw": amount,
                "value_억원": krw_to_eok(amount),
                "yoy_growth_pct": item.get("yoy_growth_pct"),
            }
        )
    return series


def brand_series_10pt(
    data: dict[str, Any],
    brand: str,
    krw_to_eok: Callable[[Any], float | None],
) -> list[dict[str, Any]]:
    brand_block = _brand_block(data)
    if not isinstance(brand_block, dict):
        return []
    periods = brand_block.get("periods_10pt")
    values = brand_block.get("values")
    if not isinstance(periods, list) or not values or not isinstance(values[0], dict):
        return []
    brand_rows = values[0].get("brands_in_value")
    if not isinstance(brand_rows, list):
        return []
    row = next((item for item in brand_rows if isinstance(item, dict) and item.get("brand") == brand), None)
    if not isinstance(row, dict):
        return []
    return _series_from_brand_row(periods, row, krw_to_eok)


def top_brand_trend_series(
    data: dict[str, Any],
    krw_to_eok: Callable[[Any], float | None],
    *,
    include_brands: tuple[str, ...] = (),
    top_n: int = 5,
) -> list[dict[str, Any]]:
    brand_block = _brand_block(data)
    periods = brand_block.get("periods_10pt") if isinstance(brand_block, dict) else None
    values = brand_block.get("values") if isinstance(brand_block, dict) else None
    if not isinstance(periods, list) or not values or not isinstance(values[0], dict):
        return []
    raw_rows = values[0].get("brands_in_value")
    if not isinstance(raw_rows, list):
        return []
    brand_rows = [row for row in raw_rows if isinstance(row, dict) and row.get("brand")]
    ranked = sorted(enumerate(brand_rows), key=lambda item: (_rank_key(item[1]), item[0]))
    selected: list[dict[str, Any]] = [row for _, row in ranked[:top_n]]
    selected_brands = {str(row.get("brand")) for row in selected}
    for row in brand_rows:
        brand = str(row.get("brand") or "")
        if brand in include_brands and brand not in selected_brands:
            selected.append(row)
            selected_brands.add(brand)

    result: list[dict[str, Any]] = []
    for row in selected:
        series = _series_from_brand_row(periods, row, krw_to_eok)
        if len(series) < 2:
            continue
        first = series[0]
        latest = series[-1]
        share_delta = _delta(latest.get("ms_pct"), first.get("ms_pct"))
        value_delta = _delta(latest.get("value_krw"), first.get("value_krw"))
        result.append(
            {
                "brand": str(row.get("brand")),
                "rank": row.get("rank") or latest.get("rank"),
                "value_recent": latest.get("value_krw"),
                "value_recent_억원": latest.get("value_억원"),
                "ms_recent_pct": latest.get("ms_pct"),
                "share_delta_pctp": share_delta,
                "value_delta_krw": value_delta,
                "value_delta_억원": krw_to_eok(value_delta),
                "series": series,
            }
        )
    return result


def _brand_block(data: dict[str, Any]) -> Any:
    return (
        data.get("level_top5_trend", {})
        .get("by_level", {})
        .get("Brand", {})
    )


def _series_from_brand_row(
    periods: list[Any],
    row: dict[str, Any],
    krw_to_eok: Callable[[Any], float | None],
) -> list[dict[str, Any]]:

    value_series = row.get("value_series_10pt")
    ms_series = row.get("ms_series_10pt")
    rank_series = row.get("rank_series_10pt")
    if not isinstance(value_series, list):
        return []

    result: list[dict[str, Any]] = []
    for index, period in enumerate(periods):
        amount = value_series[index] if index < len(value_series) else None
        result.append(
            {
                "period": str(period),
                "value_krw": amount,
                "value_억원": krw_to_eok(amount),
                "ms_pct": ms_series[index] if isinstance(ms_series, list) and index < len(ms_series) else None,
                "rank": rank_series[index] if isinstance(rank_series, list) and index < len(rank_series) else None,
            }
        )
    return result


def _rank_key(row: dict[str, Any]) -> float:
    value = row.get("rank")
    return float(value) if isinstance(value, int | float) else 9999.0


def _delta(end: Any, start: Any) -> float | None:
    if not isinstance(end, int | float) or not isinstance(start, int | float):
        return None
    return round(float(end) - float(start), 4)
