"""Cause response sections computed from runtime dynamic-market histories."""

from __future__ import annotations

from typing import Any

from pipeline.scripts.api.dynamic_market.cause_time import (
    brand_cagr,
    history,
    latest_market_value,
    market_size_series,
    period_delta,
    period_years,
    safe_pct,
    year_totals_by_brand,
)
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, BrandMetric


def matrix_rows(*, metrics: AggregatedMetrics, focus: BrandMetric | None) -> list[dict[str, Any]]:
    """Build EI/MS and growth/MS entries used by two cause matrix cards."""

    latest_market = latest_market_value(market_size_series(metrics))
    rows: list[dict[str, Any]] = []
    for brand in metrics.all_brands:
        hist = history(brand)
        latest_value = brand.latest_value or 0.0
        share = (latest_value / latest_market * 100) if latest_market else 0.0
        brand_growth = brand_cagr(hist)
        ei = (brand_growth / metrics.cagr * 100) if brand_growth is not None and metrics.cagr not in (None, 0) else None
        contribution = period_delta(hist)
        row = {
            "brand": brand.brand_name,
            "brand_key": brand.brand_key,
            "company": brand.brand_name,
            "is_target": bool(focus and brand.brand_key == focus.brand_key),
            "is_jw": False,
            "is_others": False,
            "rank": brand.rank,
            "rank_overall": brand.rank,
            "value_recent": latest_value,
            "raw_value": latest_value,
            "share_pct": share,
            "ms_recent_pct": share,
            "ms_pct": share,
            "ei": ei,
            "ei_5y": ei,
            "cagr_5y_pct": brand_growth,
            "brand_cagr_pct": brand_growth,
            "market_cagr_pct": metrics.cagr,
            "ei_basis": "brand CAGR / market CAGR * 100",
            "ei_period_years": period_years(hist),
            "ei_note": "동적 시장은 runtime filter로 정의된다.",
            "cagr_basis": "first positive month to latest month",
            "momentum_score": _momentum(hist),
            "growth_contribution": contribution,
            "growth_contribution_pct": None,
            "contribution": contribution,
            "contribution_pct": None,
        }
        rows.append(row)
    return rows[:100]


def display_matrix_rows(
    rows: list[dict[str, Any]],
    *,
    focus: BrandMetric | None,
    top_n: int = 5,
) -> list[dict[str, Any]]:
    """Select the portal-visible matrix rows without mutating row values.

    The cached /api/cause builder pins the target brand first, then appends the
    top five competitors by recent value, and intentionally omits the others
    aggregate for these two matrix cards.
    """

    focus_key = focus.brand_key if focus else None
    target = next((row for row in rows if focus_key and row.get("brand_key") == focus_key), None)
    competitors = [
        row
        for row in sorted(rows, key=lambda item: float(item.get("value_recent") or 0.0), reverse=True)
        if row is not target
    ]
    selected = ([target] if target else []) + competitors[:top_n]
    return [row for row in selected if row is not None]


def brand_ranking(brands: tuple[BrandMetric, ...], *, focus: BrandMetric | None) -> dict[str, Any]:
    return _ranking(brands, focus=focus, entity_key="brand", name_attr="brand_name")


def company_ranking(brands: tuple[BrandMetric, ...]) -> dict[str, Any]:
    return _ranking(brands, focus=None, entity_key="company", name_attr="brand_name")


def growth_contribution(brands: tuple[BrandMetric, ...], *, focus: BrandMetric | None) -> dict[str, Any]:
    periods = sorted({str(point["period"]) for brand in brands for point in brand.monthly_series})
    if len(periods) < 2:
        return _empty_growth()
    start, end = periods[0], periods[-1]
    market_start = sum(history(brand).get(start, 0.0) for brand in brands)
    market_end = sum(history(brand).get(end, 0.0) for brand in brands)
    contributors = [_contributor(brand, start=start, end=end, focus=focus, market_growth=market_end - market_start) for brand in brands]
    company_contributors = [_company_contributor(item) for item in contributors]
    ranked = sorted(contributors, key=lambda item: abs(float(item["contribution"])), reverse=True)
    ranked_companies = sorted(company_contributors, key=lambda item: abs(float(item["contribution"])), reverse=True)
    payload = {
        "period_start": start,
        "period_end": end,
        "market_start": market_start,
        "market_end": market_end,
        "market_growth": market_end - market_start,
        "by_brand": {"top_contributors": ranked[:8], "others_total": sum(c["contribution"] for c in ranked[8:])},
        "by_company": {"top_contributors": ranked_companies[:8], "others_total": sum(c["contribution"] for c in ranked_companies[8:])},
    }
    payload["windows"] = {key: dict(payload) for key in ("1y", "2y", "3y", "4y", "5y")}
    return payload


def kpi(
    *,
    metrics: AggregatedMetrics,
    matrix: list[dict[str, Any]],
    focus: BrandMetric | None,
    hhi_recent: float | None,
) -> dict[str, Any]:
    target = next((item for item in matrix if focus and item["brand_key"] == focus.brand_key), matrix[0] if matrix else {})
    top3_share = sum(item.get("share_pct", 0.0) for item in matrix[:3])
    return {
        "market_size_recent": latest_market_value(market_size_series(metrics)),
        "market_cagr_5y_pct": metrics.cagr,
        "top3_share_pct": top3_share,
        "hhi_recent": hhi_recent,
        "direct_competition_count": len(metrics.all_brands),
        "target_brand": target.get("brand"),
        "target_company": target.get("company"),
        "target_ei": target.get("ei"),
        "ei": target.get("ei"),
        "ei_basis": target.get("ei_basis"),
        "ei_period_years": target.get("ei_period_years"),
        "ei_note": target.get("ei_note"),
        "brand_cagr_pct": target.get("brand_cagr_pct"),
        "market_cagr_pct": metrics.cagr,
        "target_momentum": target.get("momentum_score"),
        "target_rank": target.get("rank"),
        "target_share_pct": target.get("share_pct"),
        "brand_value_recent": target.get("value_recent"),
        "brand_share_pct": target.get("share_pct"),
        "momentum_score": target.get("momentum_score"),
    }


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
    for year, totals in sorted(year_totals_by_brand(brands).items()):
        market = sum(totals.values())
        ranked = sorted(brands, key=lambda item: (-totals.get(item.brand_key, 0.0), item.brand_key))
        rows, kept = [], set()
        for rank, brand in enumerate(ranked, start=1):
            value = totals.get(brand.brand_key, 0.0)
            if rank <= 5 or (focus and brand.brand_key == focus.brand_key):
                kept.add(brand.brand_key)
                row = _ranking_row(brand, value, market, rank, entity_key, getattr(brand, name_attr), focus)
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


def _contributor(brand: BrandMetric, *, start: str, end: str, focus: BrandMetric | None, market_growth: float) -> dict[str, Any]:
    hist = history(brand)
    start_value = hist.get(start, 0.0)
    end_value = hist.get(end, 0.0)
    delta = end_value - start_value
    return {
        "brand": brand.brand_name,
        "company": brand.brand_name,
        "is_target": bool(focus and brand.brand_key == focus.brand_key),
        "is_jw": False,
        "is_others": False,
        "contribution": delta,
        "contribution_value": delta,
        "contribution_pct": safe_pct(delta, market_growth),
        "value_start": start_value,
        "value_end": end_value,
        "value_recent": end_value,
    }


def _company_contributor(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "company": row["company"],
        "is_target": row["is_target"],
        "is_jw": row["is_jw"],
        "is_others": row["is_others"],
        "contribution": row["contribution"],
        "contribution_value": row["contribution_value"],
        "contribution_pct": row["contribution_pct"],
        "value_recent": row["value_recent"],
        "brands": [row["brand"]],
    }


def _empty_growth() -> dict[str, Any]:
    base = {"top_contributors": [], "others_total": 0.0}
    return {"by_brand": base, "by_company": base, "market_start": 0.0, "market_end": 0.0, "market_growth": 0.0}


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


def _momentum(hist: dict[str, float]) -> float | None:
    values = [value for _, value in sorted(hist.items())]
    if len(values) < 4:
        return None
    baseline = sum(values[-4:-1]) / 3
    return safe_pct(values[-1] - baseline, baseline)
