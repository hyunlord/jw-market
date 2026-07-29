from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Any

from .market_target_competition import _metric, compute_target_competition_iqvia, compute_target_competition_ubist

def _periods(rows: list[dict[str, Any]]) -> list[str]:
    found: set[str] = set()
    for row in rows:
        found.update((row.get("raw_value_history") or {}).keys())
    return sorted(found)

def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    text = str(value).strip()
    return text != "" and text.lower() not in {"nan", "none", "null"}

def _truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return bool(value)

def compute_market_size_series(rows: list[dict[str, Any]]) -> dict[str, float]:
    series: dict[str, float] = defaultdict(float)
    for row in sorted(rows, key=lambda item: str(item.get("brand_key") or "")):
        for period, value in (row.get("raw_value_history") or {}).items():
            series[period] += float(value or 0)
    return dict(sorted(series.items()))

def compute_hhi_series(rows: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for period in _periods(rows):
        values = [
            float(_metric(row, period).get("raw_value") or 0)
            for row in sorted(
                rows,
                key=lambda item: str(item.get("brand_key") or ""),
            )
        ]
        total = sum(values)
        if total <= 0:
            result[period] = 0.0
            continue
        result[period] = sum((value / total * 100) ** 2 for value in values if value > 0)
    return result

def compute_brand_ranking_stacked(rows: list[dict[str, Any]], top_n: int = 20) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for period in _periods(rows):
        items = []
        total = sum(float(_metric(row, period).get("raw_value") or 0) for row in rows)
        for row in rows:
            value = float(_metric(row, period).get("raw_value") or 0)
            if value <= 0:
                continue
            items.append(
                {
                    "brand_key": row.get("brand_key"),
                    "brand": row.get("brand_name"),
                    "rank": 0,
                    "raw_value": value,
                    "ms": value / total * 100 if total else 0.0,
                }
            )
        items.sort(
            key=lambda item: (
                -float(item["raw_value"]),
                str(item.get("brand_key") or ""),
            )
        )
        for idx, item in enumerate(items, start=1):
            item["rank"] = idx
        result[period] = items[:top_n]
    return result

def compute_company_ranking_stacked(rows: list[dict[str, Any]], top_n: int = 20) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for period in _periods(rows):
        company_values: dict[str, float] = defaultdict(float)
        for row in rows:
            dim = row.get("by_dimension") or {}
            company = dim.get("company") or dim.get("manufacturer") or "Unknown"
            company_values[str(company)] += float(_metric(row, period).get("raw_value") or 0)
        total = sum(company_values.values())
        ranked = []
        for idx, (company, value) in enumerate(
            sorted(company_values.items(), key=lambda item: (-item[1], item[0])),
            start=1,
        ):
            if value <= 0:
                continue
            ranked.append({"company": company, "rank": idx, "raw_value": value, "ms": value / total * 100 if total else 0.0})
        result[period] = ranked[:top_n]
    return result

def compute_company_concentration_trend(company_ranking: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for period, ranking in company_ranking.items():
        shares = [float(item.get("ms") or 0) for item in ranking]
        result[period] = {
            "cr4": sum(shares[:4]),
            "cr10": sum(shares[:10]),
            "company_count": len(ranking),
        }
    return result

def compute_ei_ms_matrix(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        periods = sorted((row.get("metric_history") or {}).keys())
        if not periods:
            continue
        period = periods[-1]
        metric = _metric(row, period)
        ext = (row.get("extended_metric_history") or {}).get(period, {}) or {}
        result.append(
            {
                "brand_key": row.get("brand_key"),
                "brand": row.get("brand_name"),
                "period": period,
                "ms": metric.get("ms"),
                "ei_5y": ext.get("ei_5y"),
                "momentum_score": ext.get("momentum_score"),
            }
        )
    return result

def compute_growth_contribution_ms_matrix(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        periods = sorted((row.get("metric_history") or {}).keys())
        if not periods:
            continue
        period = periods[-1]
        metric = _metric(row, period)
        ext = (row.get("extended_metric_history") or {}).get(period, {}) or {}
        result.append(
            {
                "brand_key": row.get("brand_key"),
                "brand": row.get("brand_name"),
                "period": period,
                "ms": metric.get("ms"),
                "growth_contribution": ext.get("growth_contribution"),
                "growth_contribution_pct": ext.get("growth_contribution_pct"),
            }
        )
    return result

def compute_growth_contribution(rows: list[dict[str, Any]], top_n: int = 20) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for period in _periods(rows):
        items = []
        for row in rows:
            ext = (row.get("extended_metric_history") or {}).get(period, {}) or {}
            value = ext.get("growth_contribution")
            if value is None:
                continue
            items.append({"brand_key": row.get("brand_key"), "brand": row.get("brand_name"), "growth_contribution": value})
        items.sort(key=lambda item: abs(float(item["growth_contribution"] or 0)), reverse=True)
        result[period] = items[:top_n]
    return result

def compute_analysis_levels(rows: list[dict[str, Any]], catalog_market_row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not catalog_market_row:
        return None
    levels = {}
    for key in ("class", "molecule", "dosage_form", "strength_pack", "nhi_type", "ox_gx", "fish_oil"):
        flag = catalog_market_row.get(f"analyze_{key}")
        if not flag:
            continue
        values: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for row in rows:
            if key == "class" and _truthy((row.get("overlay_data") or {}).get("is_class_excluded")):
                continue
            dim = row.get("by_dimension") or {}
            label = dim.get(key) or (row.get("overlay_data") or {}).get(key)
            if not label:
                continue
            for period, value in (row.get("raw_value_history") or {}).items():
                values[str(label)][period] += float(value or 0)
        levels[key] = {label: dict(sorted(series.items())) for label, series in values.items()}
    return levels

def compute_level_top5_trend(analysis_levels: dict[str, Any] | None) -> dict[str, Any] | None:
    if not analysis_levels:
        return None
    result = {}
    for level, labels in analysis_levels.items():
        periods = sorted({period for series in labels.values() for period in series.keys()})
        result[level] = {}
        for period in periods:
            ranked = sorted(
                ({"label": label, "raw_value": float(series.get(period) or 0)} for label, series in labels.items()),
                key=lambda item: (-item["raw_value"], item["label"]),
            )
            result[level][period] = ranked[:5]
    return result

def compute_market_mart_payload(
    rows: list[dict[str, Any]],
    source: str,
    measure: str,
    view_type: str,
    catalog_market_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    company_ranking = compute_company_ranking_stacked(rows)
    analysis_levels = compute_analysis_levels(rows, catalog_market_row) if view_type != "general" else None
    level_top5 = compute_level_top5_trend(analysis_levels) if analysis_levels else None
    payload = {
        "view_type": view_type,
        "etl_version": "v3.1",
        "computed_at": datetime.now().isoformat(timespec="seconds"),
        "brand_rows": len(rows),
    }
    if view_type == "general":
        payload["analysis_levels_status"] = "not_applicable"
        payload["level_top5_trend_status"] = "not_applicable"
    target = (
        compute_target_competition_ubist(rows, catalog_market_row)
        if source == "ubist"
        else compute_target_competition_iqvia(rows, catalog_market_row)
    )
    return {
        "market_size_series": compute_market_size_series(rows),
        "hhi_series_5y": compute_hhi_series(rows),
        "brand_ranking_stacked": compute_brand_ranking_stacked(rows),
        "company_ranking_stacked": company_ranking,
        "company_concentration_trend": compute_company_concentration_trend(company_ranking),
        "ei_ms_matrix": compute_ei_ms_matrix(rows),
        "growth_contribution_ms_matrix": compute_growth_contribution_ms_matrix(rows),
        "growth_contribution": compute_growth_contribution(rows),
        "analysis_levels": analysis_levels,
        "level_top5_trend": level_top5,
        "target_customer_competition": target,
        "payload": payload,
    }


def compute_market_mart_payload_from_reduced_rows(
    rows: list[dict[str, Any]],
    *,
    source: str,
    measure: str,
    view_type: str,
    catalog_market_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute ATC-global derivatives only after partition partials are reduced."""
    return compute_market_mart_payload(
        rows,
        source=source,
        measure=measure,
        view_type=view_type,
        catalog_market_row=catalog_market_row,
    )
