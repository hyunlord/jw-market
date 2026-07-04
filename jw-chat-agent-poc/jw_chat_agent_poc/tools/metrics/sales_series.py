from __future__ import annotations

from typing import Any

from jw_chat_agent_poc.agentic import MetricFilterPlan
from jw_chat_agent_poc.tools.metrics.sales_filter_values import krw_to_eok, num


def market_series(data: dict[str, Any]) -> list[dict[str, Any]]:
    value = ((data.get("sources_data") or {}) if isinstance(data.get("sources_data"), dict) else {}).get("market_size_series")
    if not isinstance(value, dict):
        return []
    rows: list[dict[str, Any]] = []
    for period in sorted(value):
        item = value.get(period)
        amount = num(item.get("value")) if isinstance(item, dict) else None
        rows.append({"period": str(period), "value": amount, "value_krw": amount, "value_억원": krw_to_eok(amount), "yoy_growth_pct": item.get("yoy_growth_pct") if isinstance(item, dict) else None})
    return rows


def brand_series(data: dict[str, Any], brand: str) -> list[dict[str, Any]]:
    block = (((data.get("level_top5_trend") or {}).get("by_level") or {}).get("Brand") or {})
    periods = block.get("periods_10pt") if isinstance(block, dict) else None
    values = block.get("values") if isinstance(block, dict) else None
    if not isinstance(periods, list) or not isinstance(values, list) or not values:
        return []
    rows = values[0].get("brands_in_value") if isinstance(values[0], dict) else None
    row = _brand_row(rows if isinstance(rows, list) else [], brand)
    series = row.get("value_series_10pt")
    ms_series = row.get("ms_series_10pt")
    rank_series = row.get("rank_series_10pt")
    if not isinstance(series, list):
        return []
    out: list[dict[str, Any]] = []
    for index, period in enumerate(periods):
        amount = num(series[index]) if index < len(series) else None
        out.append({"period": str(period), "value": amount, "value_krw": amount, "value_억원": krw_to_eok(amount), "ms_pct": ms_series[index] if isinstance(ms_series, list) and index < len(ms_series) else None, "rank": rank_series[index] if isinstance(rank_series, list) and index < len(rank_series) else None})
    return out


def select_periods(rows: list[dict[str, Any]], plan: MetricFilterPlan, explicit_periods: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    if explicit_periods:
        wanted = set(explicit_periods)
        return [row for row in rows if row.get("period") in wanted]
    if plan.period_month:
        return [row for row in rows if row.get("period") == plan.period_month]
    year = plan.period_year
    if plan.period == "previous_year":
        years = sorted({int(str(row.get("period"))[:4]) for row in rows if str(row.get("period"))[:4].isdigit()})
        year = years[-1] - 1 if years else None
    if year is not None:
        return [row for row in rows if str(row.get("period", "")).startswith(str(year))]
    return rows[-1:] if rows else []


def resolved_year(*row_groups: list[dict[str, Any]], plan: MetricFilterPlan) -> int | None:
    if plan.period_year is not None:
        return plan.period_year
    if plan.period != "previous_year":
        return None
    years = [int(str(row.get("period"))[:4]) for rows in row_groups for row in rows if str(row.get("period"))[:4].isdigit()]
    return years[0] if years else None


def period_label(market_rows: list[dict[str, Any]], brand_rows: list[dict[str, Any]], plan: MetricFilterPlan, year: int | None) -> str:
    if plan.period_month:
        return plan.period_month
    if year is not None:
        return str(year)
    rows = brand_rows or market_rows
    return str(rows[-1].get("period")) if rows else "latest"


def latest_period_label(*row_groups: list[dict[str, Any]]) -> str:
    periods = sorted(str(row.get("period")) for rows in row_groups for row in rows if row.get("period"))
    return periods[-1] if periods else ""


def _brand_row(rows: list[Any], brand: str) -> dict[str, Any]:
    for row in rows:
        if isinstance(row, dict) and (row.get("brand") == brand or row.get("name") == brand):
            return row
    return {}
