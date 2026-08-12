from __future__ import annotations

import math
import re
from typing import Any

from .mart_metric_reader import MlMetricRows, json_load


def optional_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _period_key(value: str) -> tuple[int, int, int, str]:
    text = str(value)
    month = re.match(r"^(\d{4})-(\d{2})$", text)
    if month:
        return int(month.group(1)), int(month.group(2)), 0, text
    quarter = re.match(r"^(\d{4})-Q([1-4])$", text)
    if quarter:
        return int(quarter.group(1)), int(quarter.group(2)) * 3, 1, text
    return 0, 0, 0, text


def _period_ordinal(period: str) -> tuple[int, int] | None:
    month = re.match(r"^(\d{4})-(\d{2})$", str(period))
    if month:
        value = int(month.group(2))
        if 1 <= value <= 12:
            return int(month.group(1)) * 12 + (value - 1), 12
        return None
    quarter = re.match(r"^(\d{4})-Q([1-4])$", str(period))
    if quarter:
        return int(quarter.group(1)) * 4 + (int(quarter.group(2)) - 1), 4
    return None


def _period_year(period: str) -> int | None:
    try:
        return int(str(period)[:4])
    except (TypeError, ValueError):
        return None


def _period_value(item: Any) -> float | None:
    if isinstance(item, dict):
        for key in ("raw_value", "value", "market_size", "sales"):
            if key in item:
                return optional_float(item[key])
        return None
    return optional_float(item)


def _latest_pair(series: dict[str, Any] | None) -> tuple[str | None, Any]:
    data = series or {}
    if not data:
        return None, {}
    period = sorted(data.keys(), key=_period_key)[-1]
    return str(period), data[period]


def _latest_number(series: dict[str, Any] | None) -> float | None:
    _period, item = _latest_pair(series)
    return _period_value(item)


def _metric_history(row: dict[str, Any]) -> dict[str, Any]:
    value = json_load(row.get("metric_history"))
    return value if isinstance(value, dict) else {}


def _extended_metric_history(row: dict[str, Any]) -> dict[str, Any]:
    value = json_load(row.get("extended_metric_history"))
    return value if isinstance(value, dict) else {}


def _latest_history_item(row: dict[str, Any]) -> dict[str, Any]:
    _period, item = _latest_pair(_metric_history(row))
    return item if isinstance(item, dict) else {}


def _latest_extended_item(row: dict[str, Any]) -> dict[str, Any]:
    _period, item = _latest_pair(_extended_metric_history(row))
    return item if isinstance(item, dict) else {}


def _calculate_cagr(start_value: Any, end_value: Any, years: int) -> float | None:
    start = optional_float(start_value)
    end = optional_float(end_value)
    if start is None or end is None or years <= 0:
        return None
    if start == 0 and end == 0:
        return 0.0
    if start == 0 and end > 0:
        return None
    if start > 0 and end == 0:
        return -100.0
    if start < 0 or end < 0:
        return None
    return ((end / start) ** (1 / years) - 1) * 100


def _endpoint_cagr(series: dict[str, Any] | None, years: int) -> dict[str, Any]:
    data = series or {}
    if len(data) < 2:
        return {"cagr_pct": None, "basis": f"endpoint_{years}y", "period_years": years, "note": "insufficient history"}
    latest_period, latest_item = _latest_pair(data)
    latest_ord = _period_ordinal(str(latest_period)) if latest_period else None
    if latest_period is None or latest_ord is None:
        return {"cagr_pct": None, "basis": f"endpoint_{years}y", "period_years": years, "note": "invalid latest period"}
    ordinal, periods_per_year = latest_ord
    target_ordinal = ordinal - periods_per_year * years
    start_period = next(
        (str(period) for period in data if (_period_ordinal(str(period)) or (None, None))[0] == target_ordinal),
        None,
    )
    latest_value = _period_value(latest_item)
    start_value = _period_value(data.get(start_period)) if start_period else None
    if start_period is None or start_value is None:
        return {
            "cagr_pct": None,
            "basis": f"endpoint_{years}y",
            "period_years": years,
            "start_period": start_period,
            "end_period": latest_period,
            "note": "missing endpoint period",
        }
    if start_value <= 0:
        return {
            "cagr_pct": None,
            "basis": f"endpoint_{years}y",
            "period_years": years,
            "start_period": start_period,
            "end_period": latest_period,
            "note": "endpoint start value is not positive",
        }
    cagr = _calculate_cagr(start_value, latest_value, years)
    return {
        "cagr_pct": round(cagr, 4) if cagr is not None else None,
        "basis": f"endpoint_{years}y",
        "period_years": years,
        "start_period": start_period,
        "end_period": latest_period,
        "start_value": start_value,
        "end_value": latest_value,
    }


def _calculate_ei_with_fallback(brand_series: dict[str, Any], market_series: dict[str, Any]) -> dict[str, Any]:
    for years in (5, 3):
        brand_meta = _endpoint_cagr(brand_series, years)
        market_meta = _endpoint_cagr(market_series, years)
        brand_cagr = brand_meta.get("cagr_pct")
        market_cagr = market_meta.get("cagr_pct")
        if brand_cagr is None or market_cagr is None or market_cagr == 0:
            continue
        return {
            "ei": round((float(brand_cagr) / float(market_cagr)) * 100, 4),
            "basis": f"endpoint_{years}y",
            "period_years": years,
            "brand_cagr_pct": round(float(brand_cagr), 4),
            "market_cagr_pct": round(float(market_cagr), 4),
            "brand_start_period": brand_meta.get("start_period"),
            "brand_end_period": brand_meta.get("end_period"),
            "market_start_period": market_meta.get("start_period"),
            "market_end_period": market_meta.get("end_period"),
        }
    return {"ei": None, "basis": "endpoint_na", "note": "5년/3년 endpoint CAGR 산출 불가 — N/A"}


def _market_series(row: dict[str, Any]) -> dict[str, Any]:
    value = json_load(row.get("market_size_series"))
    return value if isinstance(value, dict) else {}


def _recent_value(row: dict[str, Any]) -> float:
    recent = _latest_history_item(row)
    return optional_float(recent.get("raw_value") or recent.get("value")) or 0.0


def _market_total(rows: MlMetricRows) -> float | None:
    total = _latest_number(_market_series(rows.market_row))
    if total is not None and total > 0:
        return total
    fallback = sum(_recent_value(row) for row in rows.sibling_rows)
    return fallback if fallback > 0 else None


def _annual_share_hhi_from_rows(rows: tuple[dict[str, Any], ...], source: str) -> list[dict[str, Any]]:
    expected = 12 if source.upper() == "UBIST" else 4 if source.upper() == "IQVIA" else None
    values_by_year: dict[int, dict[str, float]] = {}
    periods_by_year: dict[int, set[str]] = {}
    for row in rows:
        brand = str(row.get("brand_name") or row.get("brand_key") or "")
        if not brand:
            continue
        for period, item in _metric_history(row).items():
            year = _period_year(str(period))
            if year is None:
                continue
            values_by_year.setdefault(year, {})[brand] = values_by_year.setdefault(year, {}).get(brand, 0.0) + (
                _period_value(item) or 0.0
            )
            periods_by_year.setdefault(year, set()).add(str(period))
    complete_years = {
        year
        for year, periods in periods_by_year.items()
        if expected is None or len(periods) >= expected
    }
    points = []
    for year in sorted(year for year in values_by_year if year in complete_years)[-5:]:
        values = values_by_year[year]
        total = sum(values.values())
        hhi = sum(((value / total) * 100.0) ** 2 for value in values.values()) if total > 0 else 0.0
        points.append({"period": str(year), "period_full": str(year), "year": year, "hhi": round(hhi, 4)})
    return points


def calculate_ml_kpi_extras(rows: MlMetricRows) -> dict[str, Any]:
    """Return bundle KPI extras computed from strategic mart rows."""

    brand = str(rows.brand_row.get("brand_name") or "")
    source = "IQVIA" if str(rows.brand_row.get("source") or "").startswith("iqvia") else str(rows.brand_row.get("source") or "").upper()
    market_series = _market_series(rows.market_row)
    market_total = _market_total(rows)
    recent = _latest_history_item(rows.brand_row)
    extended = _latest_extended_item(rows.brand_row)
    target_value = _recent_value(rows.brand_row)
    target_share = round(target_value / market_total * 100.0, 4) if market_total else None
    ei_meta = _calculate_ei_with_fallback(_metric_history(rows.brand_row), market_series)
    cagr_raw = optional_float(extended.get("cagr_5y"))
    brand_cagr_5y = round(cagr_raw * 100.0, 4) if cagr_raw is not None else None
    hhi_points = _annual_share_hhi_from_rows(rows.sibling_rows, source)
    hhi_recent = optional_float(hhi_points[-1].get("hhi")) if hhi_points else _latest_number(json_load(rows.market_row.get("hhi_series_5y")))
    positive_brands = [row for row in rows.sibling_rows if _recent_value(row) > 0]
    market_avg = round(100.0 / len(positive_brands), 4) if positive_brands else None
    market_cagr_display = optional_float(ei_meta.get("market_cagr_pct"))
    if market_cagr_display is None:
        market_cagr_display = optional_float(_endpoint_cagr(market_series, 5).get("cagr_pct"))
    ranking = json_load(rows.market_row.get("brand_ranking_stacked"))
    matrix = json_load(rows.market_row.get("ei_ms_matrix"))
    sibling_count = len(
        {
            row.get("brand_key") or row.get("brand_name")
            for row in rows.sibling_rows
            if row.get("brand_key") or row.get("brand_name")
        }
    )
    catalog_count = rows.catalog_member_count or 0
    return {
        "market_size_recent": market_total,
        "market_cagr_5y_pct": market_cagr_display,
        "hhi_recent": hhi_recent,
        "hhi_series_5y": hhi_points,
        "direct_competition_count": max(sibling_count, catalog_count),
        "target_brand": brand,
        "target_company": rows.brand_row.get("company_name"),
        "target_ei": optional_float(ei_meta.get("ei")),
        "ei": optional_float(ei_meta.get("ei")),
        "ei_basis": ei_meta.get("basis"),
        "ei_period_years": ei_meta.get("period_years"),
        "ei_note": ei_meta.get("note"),
        "brand_cagr_5y_pct": brand_cagr_5y,
        "brand_cagr_pct": optional_float(ei_meta.get("brand_cagr_pct")),
        "market_cagr_pct": optional_float(ei_meta.get("market_cagr_pct")),
        "momentum_score": optional_float(extended.get("momentum_score")),
        "target_momentum": optional_float(extended.get("momentum_score")),
        "target_rank": recent.get("rank"),
        "target_share_pct": target_share,
        "brand_value_recent": target_value,
        "brand_share_pct": target_share,
        "ms_pct": target_share,
        "market_avg_ms_pct": market_avg,
        "brand_ranking_stacked": ranking if isinstance(ranking, dict) else {},
        "ei_ms_matrix": matrix if isinstance(matrix, dict) else {},
    }
