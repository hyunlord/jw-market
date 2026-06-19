"""Archive-compatible derived metric formulas for strategy recompute."""

from __future__ import annotations

from collections import defaultdict
import math
import re
from typing import Any

from pipeline.scripts.api.market_scope.periods import sort_periods


def annual_hhi_series(
    histories: dict[str, dict[str, float]],
    *,
    source: str,
) -> list[dict[str, Any]]:
    """Return archive-compatible annual HHI points for complete years."""

    annual = _annual_totals(histories)
    complete_years = _complete_calendar_years(_period_counts(histories), source=source)
    points: list[dict[str, Any]] = []
    for year in sorted(year for year in annual if year in complete_years)[-5:]:
        total = sum(annual[year].values())
        shares = [round((value / total) * 100.0, 4) for value in annual[year].values()] if total > 0 else []
        hhi = round(sum(math.pow(share, 2) for share in shares), 4) if shares else 0.0
        points.append({"period": str(year), "period_full": str(year), "year": year, "hhi": hhi})
    return points


def annual_ranking_payload(
    histories: dict[str, dict[str, float]],
    *,
    label_key: str,
    focus_id: str | None,
    display_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return the archive stacked-ranking payload from annual full-year sums."""

    annual = _annual_totals(histories)
    years = sorted(annual)[-5:]
    ranked_by_year = {year: _rank_rows(annual[year], label_key=label_key, display_names=display_names) for year in years}
    fixed_ids = _fixed_ids(ranked_by_year.get(years[-1], []), label_key=label_key, focus_id=focus_id) if years else []
    yearly = [_yearly_selection(year, ranked_by_year[year], fixed_ids, label_key=label_key) for year in years]
    labels = [*fixed_ids, "기타"]
    trend_key = "brands" if label_key == "brand_key" else "companies"
    return {
        "years": years,
        "yearly": yearly,
        trend_key: _latest_trends(years, ranked_by_year, fixed_ids, label_key=label_key),
        "top_brands": labels,
        "series": {
            label: [
                _selected_value(item["rankings"], label, label_key=label_key)
                for item in yearly
            ]
            for label in labels
        },
        "rankings_by_year": {str(year): ranked_by_year[year] for year in years},
        "period_count_by_year": {str(year): _period_counts(histories).get(year, 0) for year in years},
    }


def endpoint_ei_with_fallback(
    brand_series: dict[str, float],
    market_series: dict[str, float],
    *,
    target_years: int = 5,
) -> dict[str, Any]:
    """Calculate EI with the archive 5-year endpoint and 3-year fallback."""

    for years in (target_years, 3):
        brand_meta = _endpoint_cagr(brand_series, years)
        market_meta = _endpoint_cagr(market_series, years)
        brand_cagr = brand_meta.get("cagr_pct")
        market_cagr = market_meta.get("cagr_pct")
        if brand_cagr is None or market_cagr is None or market_cagr == 0:
            continue
        return {
            "ei": round((float(brand_cagr) / float(market_cagr)) * 100.0, 4),
            "basis": f"endpoint_{years}y",
            "period_years": years,
            "brand_cagr_pct": round(float(brand_cagr), 4),
            "market_cagr_pct": round(float(market_cagr), 4),
            "brand_start_period": brand_meta.get("start_period"),
            "brand_end_period": brand_meta.get("end_period"),
            "market_start_period": market_meta.get("start_period"),
            "market_end_period": market_meta.get("end_period"),
        }
    return {"ei": None, "basis": "endpoint_na", "note": "5y/3y endpoint CAGR is not computable"}


def ei_ms_matrix_payload(
    histories: dict[str, dict[str, float]],
    market_series: dict[str, float],
    *,
    focus_brand_key: str,
    brand_names: dict[str, str],
    companies: dict[str, str],
    top_n: int = 5,
) -> dict[str, Any]:
    """Return archive-style EI/MS matrix rows using latest-period values."""

    periods = sort_periods(market_series)
    if not periods:
        return {"data": [], "ms_avg_pct": 0.0, "share_avg_pct": 0.0}
    latest = periods[-1]
    market_total = market_series.get(latest, 0.0)
    rows = []
    for brand_key, history in histories.items():
        value = history.get(latest, 0.0)
        share = round((value / market_total * 100.0) if market_total else 0.0, 4)
        ei_meta = endpoint_ei_with_fallback(history, market_series)
        rows.append(
            {
                "brand": brand_names.get(brand_key, brand_key),
                "brand_key": brand_key,
                "company": companies.get(brand_key, "Unknown"),
                "is_target": brand_key == focus_brand_key,
                "is_jw": brand_key == focus_brand_key,
                "is_others": False,
                "rank": None,
                "rank_overall": None,
                "value_recent": value,
                "raw_value": value,
                "share_pct": share,
                "ms_pct": share,
                "ms_recent_pct": share,
                "ei": ei_meta.get("ei"),
                "ei_5y": ei_meta.get("ei"),
                "cagr_5y_pct": ei_meta.get("brand_cagr_pct"),
                "brand_cagr_pct": ei_meta.get("brand_cagr_pct"),
                "market_cagr_pct": ei_meta.get("market_cagr_pct"),
                "ei_basis": ei_meta.get("basis"),
                "ei_period_years": ei_meta.get("period_years"),
                "ei_note": ei_meta.get("note"),
                "cagr_basis": ei_meta.get("basis"),
                "momentum_score": None,
                "growth_contribution": 0.0,
                "growth_contribution_pct": 0.0,
                "contribution": 0.0,
                "contribution_pct": 0.0,
            }
        )
    ranked = sorted((row for row in rows if row["value_recent"] > 0), key=lambda item: float(item["value_recent"]), reverse=True)
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index
        row["rank_overall"] = index
    target = next((row for row in rows if row["brand_key"] == focus_brand_key), None)
    competitors = [row for row in ranked if row["brand_key"] != focus_brand_key]
    selected = ([target] if target else []) + competitors[:top_n]
    visible = [row for row in selected if row and not row.get("is_others")]
    shares = [float(row.get("share_pct") or 0.0) for row in visible]
    avg = round(sum(shares) / len(shares), 4) if shares else 0.0
    return {"data": selected, "ms_avg_pct": avg, "share_avg_pct": avg}


def _annual_totals(histories: dict[str, dict[str, float]]) -> dict[int, dict[str, float]]:
    totals: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for item_id, history in histories.items():
        for period, value in history.items():
            year = _period_year(period)
            if year is not None:
                totals[year][item_id] += value
    return {year: dict(values) for year, values in totals.items()}


def _period_counts(histories: dict[str, dict[str, float]]) -> dict[int, int]:
    periods_by_year: dict[int, set[str]] = defaultdict(set)
    for history in histories.values():
        for period in history:
            year = _period_year(period)
            if year is not None:
                periods_by_year[year].add(period)
    return {year: len(periods) for year, periods in periods_by_year.items()}


def _complete_calendar_years(period_counts: dict[int, int], *, source: str) -> set[int]:
    expected = 12 if _source_key(source) == "UBIST" else 4
    return {year for year, count in period_counts.items() if count >= expected}


def _rank_rows(values: dict[str, float], *, label_key: str, display_names: dict[str, str] | None) -> list[dict[str, Any]]:
    total = sum(values.values())
    rows = []
    for index, (item_id, value) in enumerate(sorted(values.items(), key=lambda item: (-item[1], item[0])), start=1):
        share = round(value / total * 100.0, 4) if total > 0 else 0.0
        row = {
            label_key: item_id,
            "rank": index if value > 0 else None,
            "raw_value": value,
            "value": value,
            "ms": share,
            "ms_pct": share,
            "is_target": False,
            "is_jw": False,
            "is_others": False,
        }
        if label_key == "brand_key":
            row["brand"] = (display_names or {}).get(item_id, item_id)
        else:
            row["company"] = item_id
        rows.append(row)
    return rows


def _fixed_ids(latest_rows: list[dict[str, Any]], *, label_key: str, focus_id: str | None) -> list[str]:
    ids = [str(row[label_key]) for row in latest_rows if row.get(label_key)]
    if focus_id and focus_id in ids:
        return [focus_id, *(item_id for item_id in ids if item_id != focus_id)][:6]
    return ids[:6]


def _yearly_selection(year: int, ranked_rows: list[dict[str, Any]], fixed_ids: list[str], *, label_key: str) -> dict[str, Any]:
    row_by_id = {str(row[label_key]): row for row in ranked_rows if row.get(label_key)}
    selected = [row_by_id[item_id] for item_id in fixed_ids if item_id in row_by_id]
    selected_ids = {str(row[label_key]) for row in selected}
    others = [row for row in ranked_rows if str(row.get(label_key)) not in selected_ids]
    displayed_ms = sum(float(row.get("ms_pct") or 0.0) for row in selected)
    selected.append({label_key: "기타", "brand": "기타", "company": "기타", "is_others": True, "rank": None, "raw_value": sum(float(row.get("raw_value") or 0.0) for row in others), "value": sum(float(row.get("value") or 0.0) for row in others), "ms": round(max(0.0, 100.0 - displayed_ms), 4), "ms_pct": round(max(0.0, 100.0 - displayed_ms), 4)})
    return {"year": year, "rankings": selected}


def _latest_trends(years: list[int], ranked_by_year: dict[int, list[dict[str, Any]]], fixed_ids: list[str], *, label_key: str) -> list[dict[str, Any]]:
    return [
        {
            label_key: item_id,
            "yearly_values": [
                {"year": year, "value": _selected_value(ranked_by_year[year], item_id, label_key=label_key)}
                for year in years
            ],
        }
        for item_id in fixed_ids
    ]


def _selected_value(rows: list[dict[str, Any]], item_id: str, *, label_key: str) -> float:
    return float(next((row.get("raw_value") for row in rows if row.get(label_key) == item_id), 0.0) or 0.0)


def _endpoint_cagr(series: dict[str, float], years: int) -> dict[str, Any]:
    if len(series) < 2:
        return {"cagr_pct": None, "basis": f"endpoint_{years}y", "period_years": years, "note": "insufficient history"}
    latest_period = sort_periods(series)[-1]
    latest_ord = _period_ordinal(latest_period)
    if latest_ord is None:
        return {"cagr_pct": None, "basis": f"endpoint_{years}y", "period_years": years, "note": "invalid latest period"}
    ordinal, periods_per_year = latest_ord
    target_ordinal = ordinal - (periods_per_year * years)
    start_period = next((period for period in series if (_period_ordinal(period) or (None, None))[0] == target_ordinal), None)
    start_value = series.get(start_period or "")
    latest_value = series[latest_period]
    if start_period is None or start_value is None or start_value <= 0:
        return {"cagr_pct": None, "basis": f"endpoint_{years}y", "period_years": years, "start_period": start_period, "end_period": latest_period}
    cagr = _calculate_cagr(start_value, latest_value, years)
    return {"cagr_pct": round(cagr, 4) if cagr is not None else None, "basis": f"endpoint_{years}y", "period_years": years, "start_period": start_period, "end_period": latest_period, "start_value": start_value, "end_value": latest_value}


def _calculate_cagr(start_value: float, end_value: float, years: int) -> float | None:
    if start_value == 0 and end_value == 0:
        return 0.0
    if start_value == 0 and end_value > 0:
        return None
    if start_value > 0 and end_value == 0:
        return -100.0
    if start_value < 0 or end_value < 0:
        return None
    return (math.pow(end_value / start_value, 1 / years) - 1) * 100.0


def _period_year(period: str) -> int | None:
    ordinal = _period_ordinal(period)
    return None if ordinal is None else int(period[:4])


def _period_ordinal(period: str) -> tuple[int, int] | None:
    month_match = re.fullmatch(r"(\d{4})-(\d{2})", period)
    if month_match:
        month = int(month_match.group(2))
        return (int(month_match.group(1)) * 12 + (month - 1), 12) if 1 <= month <= 12 else None
    quarter_match = re.fullmatch(r"(\d{4})-Q([1-4])", period)
    if quarter_match:
        return (int(quarter_match.group(1)) * 4 + (int(quarter_match.group(2)) - 1), 4)
    return None


def _source_key(source: str) -> str:
    normalized = source.strip().upper()
    return "UBIST" if normalized == "UBIST" else "IQVIA"
