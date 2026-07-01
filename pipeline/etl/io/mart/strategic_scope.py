from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any

from .general_history import cagr_from_history, fill_periods, mat_growth, pct_growth, value_at
from .layer3_compute_extended import compute_ei, compute_growth_contribution, compute_momentum
from .layer3_normalize import prev_month, prev_quarter_month, same_month_prev_year
from .strategic_common import merge_numeric_json_values, sum_raw_histories
from .strategic_common import row_atc4_code


def row_raw_history(row: dict[str, Any], periods: list[str]) -> dict[str, float]:
    raw_history = row.get("raw_value_history") or {}
    metric_history = row.get("metric_history") or {}
    result: dict[str, float] = {}
    for period in periods:
        value = raw_history.get(period)
        if value is None and isinstance(metric_history.get(period), dict):
            value = metric_history[period].get("raw_value")
        try:
            result[period] = float(value or 0.0)
        except (TypeError, ValueError):
            result[period] = 0.0
    return result


def recompute_market_scoped_metric_history(rows: list[dict[str, Any]]) -> None:
    periods = fill_periods(period for row in rows for period in (row.get("raw_value_history") or {}).keys())
    if not periods:
        periods = fill_periods(period for row in rows for period in (row.get("metric_history") or {}).keys())
    if not periods:
        return
    raw_by_brand = {
        str(row.get("brand_name") or row.get("brand_key") or idx): row_raw_history(row, periods)
        for idx, row in enumerate(rows)
    }
    market_history = {period: sum(history.get(period, 0.0) for history in raw_by_brand.values()) for period in periods}
    rank_by_period: dict[str, dict[str, int]] = {}
    for period in periods:
        ranked = sorted(
            ((brand, history.get(period, 0.0)) for brand, history in raw_by_brand.items() if history.get(period, 0.0) > 0),
            key=lambda item: (-item[1], item[0]),
        )
        rank_by_period[period] = {brand: idx + 1 for idx, (brand, _) in enumerate(ranked)}
    for idx, row in enumerate(rows):
        brand_name = str(row.get("brand_name") or row.get("brand_key") or idx)
        history = raw_by_brand[brand_name]
        metric_history = dict(row.get("metric_history") or {})
        extended_history = dict(row.get("extended_metric_history") or {})
        ms_values: list[float] = []
        for period in periods:
            value = history.get(period, 0.0)
            market_total = market_history.get(period, 0.0)
            ms_pct = (value / market_total * 100.0) if market_total > 0 else 0.0
            ms_values.append(ms_pct)
            prev = value_at(history, prev_month(period))
            prev_q = value_at(history, prev_quarter_month(period))
            prev_y = value_at(history, same_month_prev_year(period))
            market_prev_y = value_at(market_history, same_month_prev_year(period))
            growth_abs = value - prev_y if prev_y is not None else None
            market_growth_abs = market_history.get(period, 0.0) - market_prev_y if market_prev_y is not None else None
            growth_contribution, gc_warning = compute_growth_contribution(growth_abs, market_growth_abs)
            cagr_5y = cagr_from_history(history, period, 5)
            market_cagr_5y = cagr_from_history(market_history, period, 5)
            ei_5y, ei_warning = compute_ei(cagr_5y, market_cagr_5y)
            metric_payload = dict(metric_history.get(period) or {})
            metric_payload.update(
                {
                    "raw_value": value,
                    "ms": ms_pct,
                    "mom": pct_growth(value, prev),
                    "qoq": pct_growth(value, prev_q),
                    "yoy": pct_growth(value, prev_y),
                    "mat": mat_growth(history, period),
                    "growth_abs": growth_abs,
                    "rank": rank_by_period[period].get(brand_name) if value > 0 else None,
                }
            )
            metric_history[period] = metric_payload
            extended_payload = dict(extended_history.get(period) or {})
            extended_payload.update(
                {
                    "cagr_1y": cagr_from_history(history, period, 1),
                    "cagr_3y": cagr_from_history(history, period, 3),
                    "cagr_5y": cagr_5y,
                    "ei_5y": ei_5y,
                    "momentum_score": compute_momentum(ms_values[-4:]) if len(ms_values) >= 4 else None,
                    "growth_contribution": growth_contribution,
                    "growth_contribution_pct": growth_contribution,
                    "market_cagr_5y": market_cagr_5y,
                    "warnings": [warning for warning in (gc_warning, ei_warning) if warning],
                }
            )
            extended_history[period] = extended_payload
        row["raw_value_history"] = history
        row["metric_history"] = metric_history
        row["extended_metric_history"] = extended_history


def group_by_source_measure(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get("source")), str(row.get("measure")))].append(row)
    return grouped


def collapse_same_rows(rows: list[dict[str, Any]], key_fields: tuple[str, str]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get(key_fields[0])), str(row.get(key_fields[1])), str(row.get("source")), str(row.get("measure")))].append(row)
    collapsed: list[dict[str, Any]] = []
    for members in grouped.values():
        if len(members) == 1:
            collapsed.append(members[0])
            continue
        base = deepcopy(members[0])
        base["raw_value_history"] = sum_raw_histories(members)
        for column in (
            "channel_data",
            "specialty_data",
            "dimension_data",
            "dimension_channel_data",
            "dimension_specialty_data",
            "channel_specialty_matrix",
            "ubist_channel_by_display",
            "ubist_channel_by_code",
        ):
            merged: Any = {}
            for member in members:
                merged = merge_numeric_json_values(merged, member.get(column) or {})
            base[column] = merged
        atc4_codes = sorted({row_atc4_code(member) for member in members if row_atc4_code(member)})
        overlay = dict(base.get("overlay_data") or {})
        overlay["collapsed_from_atc4_codes"] = atc4_codes
        overlay["collapsed_row_count"] = len(members)
        base["overlay_data"] = overlay
        collapsed.append(base)
    return collapsed
