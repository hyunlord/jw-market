"""Shared time-series helpers for dynamic cause-compatible metrics."""

from __future__ import annotations

from collections import defaultdict
import math
from typing import Any, Iterable

from pipeline.scripts.api.dynamic_market.aggregator import month_distance
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, BrandMetric
from pipeline.scripts.api.market_growth import compound_period_growth_pct


SOURCE_LABELS = {"ubist": "UBIST", "iqvia_nsa": "IQVIA"}
MEASURE_SERIES_KEY = {
    "sales": "sales_krw",
    "volume": "volume",
    "unit": "unit",
    "dosage_unit": "dosage_unit",
    "counting_unit": "counting_unit",
}
MEASURE_LABEL = {
    "sales": "처방조제액",
    "volume": "처방량",
    "unit": "Unit",
    "dosage_unit": "Dosage Unit",
    "counting_unit": "Counting Unit",
}


def history(brand: BrandMetric) -> dict[str, float]:
    """Return one brand's filtered monthly history."""

    if brand.history_by_period:
        return brand.history_by_period
    return {str(item["period"]): float(item["value"]) for item in brand.monthly_series}


def market_size_series(metrics: AggregatedMetrics) -> list[dict[str, Any]]:
    """Return cause-style market size points with annual growth metrics."""

    value_key = MEASURE_SERIES_KEY.get(metrics.measure, "value")
    totals = {str(item["period"]): float(item["market_size"]) for item in metrics.monthly_series}
    periods_per_year = 12 if metrics.source.lower() == "ubist" else 4
    series: list[dict[str, Any]] = []
    for period in sorted(totals):
        prev = previous_year_period(period)
        yoy = pct_change(totals.get(prev), totals[period]) if prev in totals else None
        mom = (
            compound_period_growth_pct(totals.get(prev), totals[period], periods_per_year) if prev in totals else None
        )
        point = {"period": period, "value": totals[period], "yoy_growth_pct": yoy, "mom_growth_pct": mom}
        point[value_key] = totals[period]
        if "sales_krw" not in point:
            point["sales_krw"] = totals[period]
        series.append(point)
    return series


def hhi_series(brands: tuple[BrandMetric, ...], *, source: str | None = None) -> list[dict[str, Any]]:
    by_year = year_totals_by_brand(brands)
    complete_years = complete_calendar_years(_period_count_by_year(brands), source=source)
    rows: list[dict[str, Any]] = []
    for year, totals in sorted(by_year.items()):
        if year not in complete_years:
            continue
        market = sum(totals.values())
        hhi = sum((round(value / market * 100, 4)) ** 2 for value in totals.values()) if market else None
        if hhi is not None:
            hhi = round(hhi, 4)
        rows.append({"period": year, "period_full": year, "year": int(year), "hhi": hhi})
    return rows


def complete_calendar_years(period_count_by_year: dict[str, int], *, source: str | None = None) -> set[str]:
    source_key = str(source or "").lower()
    expected = 12 if source_key == "ubist" else 4 if source_key == "iqvia_nsa" else None
    if expected is None:
        return set(period_count_by_year)
    return {year for year, count in period_count_by_year.items() if count >= expected}


def latest_hhi(brands: tuple[BrandMetric, ...]) -> float | None:
    latest_periods = [brand.latest_period for brand in brands if brand.latest_period]
    if not latest_periods:
        return None
    latest = max(latest_periods)
    values = [history(brand).get(latest, 0.0) for brand in brands]
    market = sum(values)
    return sum((value / market * 100) ** 2 for value in values if market) if market else None


def empty_analysis_levels(series: list[dict[str, Any]]) -> dict[str, Any]:
    periods = [str(item["period"]) for item in series]
    return {"levels": [], "channels": ["전체"], "period_unit": "월", "periods_monthly": periods, "periods_quarterly": [], "data": {}}


def coverage(series: list[dict[str, Any]]) -> dict[str, Any]:
    periods = [str(item["period"]) for item in series]
    return {"period_start": periods[0] if periods else None, "period_end": periods[-1] if periods else None, "period_count": len(periods), "period_unit": "월"}


def year_totals_by_brand(brands: tuple[BrandMetric, ...]) -> dict[str, dict[str, float]]:
    years: dict[str, dict[str, float]] = defaultdict(dict)
    for brand in brands:
        totals: dict[str, float] = defaultdict(float)
        for period, value in history(brand).items():
            totals[period[:4]] += value
        for year, value in totals.items():
            years[year][brand.brand_key] = value
    return years


def _period_count_by_year(brands: tuple[BrandMetric, ...]) -> dict[str, int]:
    periods_by_year: dict[str, set[str]] = defaultdict(set)
    for brand in brands:
        for period in history(brand):
            periods_by_year[period[:4]].add(period)
    return {year: len(periods) for year, periods in periods_by_year.items()}


def brand_cagr(values_by_period: dict[str, float]) -> float | None:
    positive = [(period, value) for period, value in sorted(values_by_period.items()) if value > 0]
    if len(positive) < 2:
        return None
    start, start_value = positive[0]
    end, end_value = positive[-1]
    months = month_distance(start, end)
    if months <= 0:
        return None
    return (math.pow(end_value / start_value, 12 / months) - 1) * 100


def period_delta(values_by_period: dict[str, float]) -> float:
    if len(values_by_period) < 2:
        return 0.0
    periods = sorted(values_by_period)
    return values_by_period[periods[-1]] - values_by_period[periods[0]]


def period_years(values_by_period: dict[str, float]) -> float | None:
    if len(values_by_period) < 2:
        return None
    periods = sorted(values_by_period)
    return month_distance(periods[0], periods[-1]) / 12


def pct_change(previous: float | None, current: float) -> float | None:
    return safe_pct(current - previous, previous) if previous not in (None, 0) else None


def safe_pct(value: float, denominator: float | None) -> float | None:
    return value / denominator * 100 if denominator not in (None, 0) else None


def previous_year_period(period: str) -> str:
    year, month = period.split("-", 1)
    return f"{int(year) - 1}-{month}"


def latest_market_value(series: list[dict[str, Any]]) -> float | None:
    return float(series[-1]["value"]) if series else None


def recent_yoy(series: list[dict[str, Any]]) -> float | None:
    return series[-1]["yoy_growth_pct"] if series else None


def avg_share(rows: list[dict[str, Any]]) -> float:
    values = [float(item.get("share_pct") or 0.0) for item in rows]
    return sum(values) / len(values) if values else 0.0


def join_unique(values: Iterable[str]) -> str:
    seen: set[str] = set()
    kept: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            kept.append(value)
    return " / ".join(kept[:8])
