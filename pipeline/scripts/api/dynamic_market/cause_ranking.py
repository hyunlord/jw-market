"""Ranking sections for runtime dynamic-market cause payloads."""

from __future__ import annotations

from typing import Any

from pipeline.scripts.api.competitor_ranking import CompetitorRankItem, select_top_competitors
from pipeline.scripts.api.dynamic_market.cause_time import history, year_totals_by_brand
from pipeline.scripts.api.dynamic_market.types import BrandMetric


def brand_ranking(brands: tuple[BrandMetric, ...], *, focus: BrandMetric | None) -> dict[str, Any]:
    return _ranking(brands, focus=focus, entity_key="brand", name_attr="brand_name")


def company_ranking(brands: tuple[BrandMetric, ...]) -> dict[str, Any]:
    return _ranking(brands, focus=None, entity_key="company", name_attr="brand_name")


def _ranking(
    brands: tuple[BrandMetric, ...],
    *,
    focus: BrandMetric | None,
    entity_key: str,
    name_attr: str,
) -> dict[str, Any]:
    yearly = []
    entity_series: dict[str, dict[str, Any]] = {}
    period_count_by_year = _period_count_by_year(brands)
    visible_brands = select_top_competitors(
        tuple(CompetitorRankItem(brand.brand_key, brand.total_value, brand) for brand in brands),
        selected_brand_key=focus.brand_key if focus else None,
        top_n=5,
    )
    for year, totals in sorted(year_totals_by_brand(brands).items()):
        market = sum(totals.values())
        ranked = sorted(brands, key=lambda item: (-totals.get(item.brand_key, 0.0), item.brand_key))
        rank_by_key = {brand.brand_key: rank for rank, brand in enumerate(ranked, start=1)}
        rows = []
        kept = set()
        for brand in visible_brands:
            value = totals.get(brand.brand_key, 0.0)
            kept.add(brand.brand_key)
            row = _ranking_row(brand, value, market, rank_by_key[brand.brand_key], entity_key, getattr(brand, name_attr), focus)
            rows.append(row)
            _collect_yearly_value(entity_series, row, entity_key, int(year))
        other_value = sum(value for key, value in totals.items() if key not in kept)
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
    brand: BrandMetric,
    value: float,
    market: float,
    rank: int,
    entity_key: str,
    name: str,
    focus: BrandMetric | None,
) -> dict[str, Any]:
    return {
        entity_key: name,
        "brand": brand.brand_name,
        "company": brand.brand_name,
        "rank": rank,
        "value": value,
        "ms_pct": value / market * 100 if market else 0.0,
        "is_target": bool(focus and brand.brand_key == focus.brand_key),
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
