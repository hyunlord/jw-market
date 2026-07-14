"""Ranking sections for runtime dynamic-market cause payloads."""

from __future__ import annotations

from collections import defaultdict
import json
from typing import Any

from pipeline.scripts.api.competitor_ranking import CompetitorRankItem, select_top_competitors
from pipeline.scripts.api.dynamic_market.cause_time import complete_calendar_years, history, year_totals_by_brand
from pipeline.scripts.api.dynamic_market.types import BrandMetric


UNCLASSIFIED_COMPANY = "미분류"


def brand_ranking(brands: tuple[BrandMetric, ...], *, focus: BrandMetric | None) -> dict[str, Any]:
    names = {brand.brand_key: brand.brand_name for brand in brands}
    companies = {brand.brand_key: _company_name(brand, fallback=brand.brand_name) for brand in brands}
    return _ranking(
        totals_by_year=year_totals_by_brand(brands),
        total_values={brand.brand_key: brand.total_value for brand in brands},
        names=names,
        companies=companies,
        focus_key=focus.brand_key if focus else None,
        entity_key="brand",
        period_count_by_year=_period_count_by_year(brands),
    )


def company_ranking(brands: tuple[BrandMetric, ...]) -> dict[str, Any]:
    totals_by_year, total_values = _company_totals(brands)
    names = {company: company for company in total_values}
    return _ranking(
        totals_by_year=totals_by_year,
        total_values=total_values,
        names=names,
        companies=names,
        focus_key=None,
        entity_key="company",
        period_count_by_year=_period_count_by_year(brands),
    )


def company_hhi_series(brands: tuple[BrandMetric, ...], *, source: str | None = None) -> list[dict[str, Any]]:
    totals_by_year, _ = _company_totals(brands)
    complete_years = complete_calendar_years(_period_count_by_year(brands), source=source)
    rows: list[dict[str, Any]] = []
    for year, totals in sorted(totals_by_year.items()):
        if year not in complete_years:
            continue
        market = sum(totals.values())
        hhi = sum((round(value / market * 100, 4)) ** 2 for value in totals.values()) if market else None
        if hhi is not None:
            hhi = round(hhi, 4)
        rows.append({"period": year, "period_full": year, "year": int(year), "hhi": hhi})
    return rows


def _company_totals(
    brands: tuple[BrandMetric, ...],
) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    totals_by_year: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    total_values: dict[str, float] = defaultdict(float)
    for brand in brands:
        company = _company_name(brand)
        total_values[company] += brand.total_value
        for period, value in history(brand).items():
            totals_by_year[period[:4]][company] += value
    return (
        {year: dict(totals) for year, totals in totals_by_year.items()},
        dict(total_values),
    )


def _ranking(
    *,
    totals_by_year: dict[str, dict[str, float]],
    total_values: dict[str, float],
    names: dict[str, str],
    companies: dict[str, str],
    focus_key: str | None,
    entity_key: str,
    period_count_by_year: dict[str, int],
) -> dict[str, Any]:
    yearly: list[dict[str, Any]] = []
    entity_series: dict[str, dict[str, Any]] = {}
    visible_keys = select_top_competitors(
        tuple(CompetitorRankItem(key, value, key) for key, value in total_values.items()),
        selected_brand_key=focus_key,
        top_n=5,
    )
    selected = set(visible_keys)
    for year, totals in sorted(totals_by_year.items()):
        market = sum(totals.values())
        annual_order = sorted(totals, key=lambda key: (-totals[key], key))
        selected_ranks = [rank for rank, key in enumerate(annual_order, start=1) if key in selected]
        max_selected_rank = max(selected_ranks, default=0)
        ordered_keys = annual_order[:max_selected_rank]
        rows = [
            _ranking_row(
                key=key,
                name=names[key],
                company=companies[key],
                value=totals.get(key, 0.0),
                market=market,
                rank=rank,
                entity_key=entity_key,
                focus_key=focus_key,
            )
            for rank, key in enumerate(ordered_keys, start=1)
        ]
        for row in rows:
            _collect_yearly_value(entity_series, row, entity_key, int(year))
        annual_visible = set(ordered_keys)
        other_value = sum(value for key, value in totals.items() if key not in annual_visible)
        if other_value:
            rows.append(_others_row(entity_key, other_value, market))
        yearly.append({"year": int(year), "rankings": rows})
    collection_key = "companies" if entity_key == "company" else "brands"
    series = list(entity_series.values())
    return {
        "years": [item["year"] for item in yearly],
        "yearly": yearly,
        collection_key: series,
        "top_brands": [str(item[entity_key]) for item in series],
        "series": {str(item[entity_key]): [point["value"] for point in item["yearly_values"]] for item in series},
        "rankings_by_year": {str(item["year"]): item["rankings"] for item in yearly},
        "period_count_by_year": period_count_by_year,
    }


def _ranking_row(
    *,
    key: str,
    name: str,
    company: str,
    value: float,
    market: float,
    rank: int,
    entity_key: str,
    focus_key: str | None,
) -> dict[str, Any]:
    return {
        entity_key: name,
        "brand": name if entity_key == "brand" else None,
        "company": company,
        "rank": rank,
        "value": value,
        "ms_pct": value / market * 100 if market else 0.0,
        "is_target": bool(focus_key and key == focus_key),
        "is_jw": False,
        "is_others": False,
    }


def _others_row(entity_key: str, value: float, market: float) -> dict[str, Any]:
    return {
        entity_key: "기타",
        "brand": "기타" if entity_key == "brand" else None,
        "company": "기타" if entity_key == "company" else None,
        "is_target": False,
        "is_jw": False,
        "is_others": True,
        "rank": None,
        "value": value,
        "ms_pct": value / market * 100 if market else 0.0,
    }


def _company_name(brand: BrandMetric, *, fallback: str = UNCLASSIFIED_COMPANY) -> str:
    value = brand.analysis_row.get("by_dimension")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return fallback
    if isinstance(value, dict):
        for key in ("company", "manufacturer", "raw_company"):
            company = value.get(key)
            if company not in (None, ""):
                return str(company)
    return fallback


def _collect_yearly_value(series: dict[str, dict[str, Any]], row: dict[str, Any], entity_key: str, year: int) -> None:
    key = str(row[entity_key])
    item = series.setdefault(
        key,
        {
            entity_key: row[entity_key],
            "brand": row.get("brand") if entity_key == "brand" else None,
            "company": row.get("company"),
            "is_target": row.get("is_target", False),
            "is_jw": row.get("is_jw", False),
            "yearly_values": [],
        },
    )
    item["yearly_values"].append({"year": year, "value": row["value"], "ms_pct": row["ms_pct"], "rank": row["rank"]})


def _period_count_by_year(brands: tuple[BrandMetric, ...]) -> dict[str, int]:
    periods_by_year: dict[str, set[str]] = {}
    for brand in brands:
        for period in history(brand):
            periods_by_year.setdefault(period[:4], set()).add(period)
    return {year: len(periods) for year, periods in sorted(periods_by_year.items())}
