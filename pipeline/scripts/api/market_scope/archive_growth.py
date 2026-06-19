"""Archive-compatible growth contribution windows for strategy recompute."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any

from pipeline.scripts.api.market_scope.periods import sort_periods


def growth_contribution_payload(
    histories: dict[str, dict[str, float]],
    *,
    source: str,
    focus_brand_key: str,
    brand_names: dict[str, str],
    companies: dict[str, str],
) -> dict[str, Any]:
    """Return archive-compatible first-to-last growth contribution windows."""

    periods = _history_periods(histories, source=source)
    payload = _growth_base_payload(histories, periods, focus_brand_key=focus_brand_key, brand_names=brand_names, companies=companies)
    windows: dict[str, dict[str, Any]] = {}
    for years in range(1, 5):
        window_periods = _growth_window_periods(periods, source=source, years=years)
        if window_periods:
            windows[f"{years}y"] = _growth_base_payload(
                histories,
                window_periods,
                focus_brand_key=focus_brand_key,
                brand_names=brand_names,
                companies=companies,
            )
    windows["5y"] = deepcopy(payload)
    payload["windows"] = windows
    return payload


def _growth_base_payload(
    histories: dict[str, dict[str, float]],
    periods: list[str],
    *,
    focus_brand_key: str,
    brand_names: dict[str, str],
    companies: dict[str, str],
) -> dict[str, Any]:
    start = periods[0] if periods else None
    end = periods[-1] if periods else None
    market_start = sum(history.get(start or "", 0.0) for history in histories.values())
    market_end = sum(history.get(end or "", 0.0) for history in histories.values())
    market_growth = market_end - market_start
    brand_rows = []
    for brand_key, history in histories.items():
        value_start = history.get(start or "", 0.0)
        value_end = history.get(end or "", 0.0)
        delta = value_end - value_start
        brand_rows.append(
            {
                "brand_key": brand_key,
                "brand": brand_names.get(brand_key, brand_key),
                "company": companies.get(brand_key, "Unknown"),
                "is_target": brand_key == focus_brand_key,
                "is_jw": brand_key == focus_brand_key,
                "is_others": False,
                "contribution": delta,
                "contribution_value": delta,
                "contribution_pct": round(delta / market_growth * 100.0, 4) if market_growth else None,
                "value_start": value_start,
                "value_end": value_end,
                "value_recent": value_end,
            }
        )
    selected = _selected_contributors(brand_rows)
    return {
        "period_start": start,
        "period_end": end,
        "market_start": market_start,
        "market_end": market_end,
        "market_growth": market_growth,
        "by_brand": {"top_contributors": selected, "others_total": 0.0},
        "by_company": _company_contribution(selected, market_growth),
    }


def _selected_contributors(rows: list[dict[str, Any]], top_n: int = 5) -> list[dict[str, Any]]:
    target = next((row for row in rows if row.get("is_target")), None)
    competitors = [
        row
        for row in sorted(rows, key=lambda item: abs(float(item.get("contribution_value") or 0.0)), reverse=True)
        if row is not target
    ]
    selected = ([target] if target else []) + competitors[:top_n]
    return [row for row in selected if row is not None]


def _company_contribution(rows: list[dict[str, Any]], market_growth: float) -> dict[str, Any]:
    grouped: dict[str, float] = defaultdict(float)
    for row in rows:
        grouped[str(row.get("company") or "Unknown")] += float(row.get("contribution_value") or 0.0)
    return {
        "top_contributors": [
            {
                "company": company,
                "contribution_value": value,
                "contribution_pct": round(value / market_growth * 100.0, 4) if market_growth else None,
            }
            for company, value in sorted(grouped.items(), key=lambda item: -abs(item[1]))
        ],
        "others_total": 0.0,
    }


def _history_periods(histories: dict[str, dict[str, float]], *, source: str) -> list[str]:
    periods = sort_periods({period for history in histories.values() for period in history})
    return list(periods[-60:] if _source_key(source) == "UBIST" else periods[-20:])


def _growth_window_periods(periods: list[str], *, source: str, years: int) -> list[str]:
    stride = 12 if _source_key(source) == "UBIST" else 4
    start_index = len(periods) - (stride * years)
    return [periods[start_index], periods[-1]] if start_index >= 0 and periods else []


def _source_key(source: str) -> str:
    normalized = source.strip().upper()
    return "UBIST" if normalized == "UBIST" else "IQVIA"
