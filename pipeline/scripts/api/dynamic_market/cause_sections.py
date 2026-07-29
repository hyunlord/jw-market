"""Cause response sections computed from runtime dynamic-market histories."""

from __future__ import annotations

from typing import Any

from pipeline.domain.momentum import compute_market_share_momentum
from pipeline.scripts.api.competitor_ranking import CompetitorRankItem, select_top_competitors
from pipeline.scripts.api.dynamic_market.cause_time import (
    brand_cagr,
    history,
    latest_market_value,
    market_size_series,
    period_delta,
    period_years,
    safe_pct,
)
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, BrandMetric
from pipeline.scripts.etl.cache_build_common import brand_cagr_exclusive, market_cagr_exclusive


def matrix_growth_value(
    growth_contribution: float | None,
    contribution_pct: float | None,
    momentum_score: float | None,
) -> float | None:
    """Return the first available matrix Y-axis value without treating zero as absent."""

    for value in (growth_contribution, contribution_pct, momentum_score):
        if value is not None:
            return value
    return None


def matrix_rows(*, metrics: AggregatedMetrics, focus: BrandMetric | None) -> list[dict[str, Any]]:
    """Build EI/MS and growth/MS entries used by two cause matrix cards."""

    market_series = market_size_series(metrics)
    market_history = {str(item["period"]): float(item["value"]) for item in market_series}
    latest_market = latest_market_value(market_series)
    market_growth = period_delta({str(item["period"]): float(item["value"]) for item in market_series})
    rows: list[dict[str, Any]] = []
    for brand in metrics.all_brands:
        hist = history(brand)
        latest_value = brand.latest_value or 0.0
        share = (latest_value / latest_market * 100) if latest_market else 0.0
        brand_growth = brand_cagr(hist)
        ei = (brand_growth / metrics.cagr * 100) if brand_growth is not None and metrics.cagr not in (None, 0) else None
        contribution = period_delta(hist)
        contribution_pct = safe_pct(contribution, market_growth)
        momentum_score = _momentum(hist, market_history)
        matrix_contribution = matrix_growth_value(contribution, contribution_pct, momentum_score)
        row = {
            "brand": brand.brand_name,
            "brand_key": brand.brand_key,
            "company": brand.brand_name,
            "is_target": bool(focus and brand.brand_key == focus.brand_key),
            "is_jw": False,
            "is_others": False,
            "rank": brand.rank,
            "rank_overall": brand.rank,
            "total_value": brand.total_value,
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
            "momentum_score": momentum_score,
            "growth_contribution": matrix_contribution,
            "growth_contribution_pct": contribution_pct,
            "contribution": contribution,
            "contribution_pct": contribution_pct,
        }
        rows.append(row)
    return rows


def display_matrix_rows(
    rows: list[dict[str, Any]],
    *,
    focus: BrandMetric | None,
    top_n: int = 5,
) -> list[dict[str, Any]]:
    """Select the portal-visible matrix rows without mutating row values.

    The cached /api/cause builder pins the target brand first, then appends the
    top five competitors by scoped total sales, and intentionally omits the
    others aggregate for these two matrix cards.
    """

    return list(
        select_top_competitors(
            tuple(
                CompetitorRankItem(
                    brand_key=str(row.get("brand_key") or ""),
                    total_value=float(row.get("total_value") or 0.0),
                    payload=row,
                )
                for row in rows
            ),
            selected_brand_key=focus.brand_key if focus else None,
            top_n=top_n,
        )
    )


def growth_contribution(
    brands: tuple[BrandMetric, ...],
    *,
    focus: BrandMetric | None,
    source: str,
) -> dict[str, Any]:
    periods = sorted({str(point["period"]) for brand in brands for point in brand.monthly_series})
    if len(periods) < 2:
        return _empty_growth()
    payload = _growth_payload(brands, periods=periods, focus=focus)
    stride = 12 if source.strip().lower() == "ubist" else 4
    windows: dict[str, dict[str, Any]] = {}
    for years in range(1, 6):
        required_points = stride * years
        truncated = len(periods) < required_points
        window_periods = periods if truncated else periods[-required_points:]
        window = _growth_payload(brands, periods=window_periods, focus=focus)
        if truncated:
            window["period_start_actual"] = window_periods[0]
            window["reason"] = "earliest_available"
        windows[f"{years}y"] = window
    payload["windows"] = windows
    return payload


def _growth_payload(
    brands: tuple[BrandMetric, ...],
    *,
    periods: list[str],
    focus: BrandMetric | None,
) -> dict[str, Any]:
    start, end = periods[0], periods[-1]
    market_start = sum(history(brand).get(start, 0.0) for brand in brands)
    market_end = sum(history(brand).get(end, 0.0) for brand in brands)
    contributors = [
        _contributor(
            brand,
            start=start,
            end=end,
            focus=focus,
            market_growth=market_end - market_start,
        )
        for brand in brands
    ]
    company_contributors = [_company_contributor(item) for item in contributors]
    ranked = sorted(contributors, key=lambda item: abs(float(item["contribution"])), reverse=True)
    ranked_companies = sorted(company_contributors, key=lambda item: abs(float(item["contribution"])), reverse=True)
    return {
        "period_start": start,
        "period_end": end,
        "market_start": market_start,
        "market_end": market_end,
        "market_growth": market_end - market_start,
        "by_brand": {"top_contributors": ranked[:8], "others_total": sum(c["contribution"] for c in ranked[8:])},
        "by_company": {
            "top_contributors": ranked_companies[:8],
            "others_total": sum(c["contribution"] for c in ranked_companies[8:]),
        },
    }


def kpi(
    *,
    metrics: AggregatedMetrics,
    matrix: list[dict[str, Any]],
    focus: BrandMetric | None,
    hhi_recent: float | None,
) -> dict[str, Any]:
    target = next((item for item in matrix if focus and item["brand_key"] == focus.brand_key), None)
    if target is None:
        return {}
    top3_share = sum(item.get("share_pct", 0.0) for item in matrix[:3])
    # Exclusive 5y/3y market CAGR: report the horizon explicitly (no silent
    # 5y→3y fallback) so the consumer can tell which window a value describes.
    # market_size_series returns a period-ordered list; the endpoint CAGR helper
    # needs a period-keyed map.
    market_series_points = market_size_series(metrics)
    market_series_map = {str(point["period"]): point for point in market_series_points}
    calculation_market_series = _calculation_market_history(metrics.all_brands)
    calculation_brand_series = _calculation_history(focus)
    market_cagr_5y, market_cagr_3y = market_cagr_exclusive(
        calculation_market_series or market_series_map
    )
    brand_cagr_5y, brand_cagr_3y = brand_cagr_exclusive(
        calculation_brand_series or history(focus)
    )
    return {
        "market_size_recent": latest_market_value(market_series_points),
        "market_cagr_5y_pct": market_cagr_5y,
        "market_cagr_3y_pct": market_cagr_3y,
        "brand_cagr_5y_pct": brand_cagr_5y,
        "brand_cagr_3y_pct": brand_cagr_3y,
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
        "target_brand_sales": target.get("value_recent"),
        "brand_value_recent": target.get("value_recent"),
        "brand_share_pct": target.get("share_pct"),
        "momentum_score": target.get("momentum_score"),
    }


def _calculation_history(brand: BrandMetric) -> dict[str, float]:
    raw = brand.analysis_row.get("calculation_metric_history")
    if not isinstance(raw, dict):
        return {}
    return {
        str(period): float(value)
        for period, value in raw.items()
    }


def _calculation_market_history(brands: tuple[BrandMetric, ...]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for brand in brands:
        for period, value in _calculation_history(brand).items():
            totals[period] = totals.get(period, 0.0) + value
    return totals


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


def _momentum(brand_history: dict[str, float], market_history: dict[str, float]) -> float | None:
    return compute_market_share_momentum(brand_history, market_history)
