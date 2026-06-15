from __future__ import annotations

from typing import Any, Iterable

import pandas as pd

from .general_config import SKU_DIMENSION_COLUMNS
from .layer3_compute_extended import compute_cagr_value, compute_hhi
from .layer3_normalize import period_range_mat, period_sort_key, prev_month, prev_quarter_month, safe_div, same_month_prev_year

def fill_periods(periods: Iterable[str]) -> list[str]:
    return sorted({str(period) for period in periods if period}, key=period_sort_key)

def period_value_map(group: pd.DataFrame, periods: list[str]) -> dict[str, float]:
    series = group.groupby("period_yyyymm", dropna=False)["raw_value"].sum().to_dict()
    return {period: float(series.get(period, 0.0) or 0.0) for period in periods}

def value_at(history: dict[str, float], period: str | None) -> float | None:
    if not period:
        return None
    return history.get(period)

def pct_growth(current: float | None, previous: float | None) -> float | None:
    ratio = safe_div(current, previous)
    if ratio is None:
        return None
    return (ratio - 1.0) * 100

def mat_growth(history: dict[str, float], period: str) -> float | None:
    window = period_range_mat(period)
    previous_end = same_month_prev_year(period)
    previous_window = period_range_mat(previous_end) if previous_end else []
    if not window or not previous_window:
        return None
    return pct_growth(sum(history.get(p, 0.0) for p in window), sum(history.get(p, 0.0) for p in previous_window))

def cagr_from_history(history: dict[str, float], period: str, years: int) -> float | None:
    try:
        ord_now = period_sort_key(period)
    except Exception:
        return None
    periods_per_year = 4 if "-Q" in period else 12
    target_ord = ord_now - periods_per_year * years
    start_period = next((p for p in history if period_sort_key(p) == target_ord), None)
    return compute_cagr_value(history.get(period), history.get(start_period) if start_period else None, years)

def hhi_for_period(part: pd.DataFrame) -> float | None:
    total = part["raw_value"].sum()
    if total <= 0:
        return None
    values = part.groupby("brand_key")["raw_value"].sum()
    return compute_hhi([(value / total) for value in values if value > 0])

def build_dimensional_history(group: pd.DataFrame, dim_col: str, periods: list[str]) -> dict[str, dict[str, dict[str, float]]]:
    if dim_col not in group.columns:
        return {}
    result: dict[str, dict[str, dict[str, float]]] = {}
    for label, part in group.groupby(dim_col, dropna=False):
        if label is None or pd.isna(label) or not str(label).strip():
            continue
        values = period_value_map(part, periods)
        result[str(label)] = {period: {"raw_value": value} for period, value in values.items()}
    return result

def build_dimension_channel_history(
    group: pd.DataFrame,
    dim_col: str,
    periods: list[str],
) -> dict[str, dict[str, dict[str, dict[str, float]]]]:
    if dim_col not in group.columns or "channel" not in group.columns:
        return {}
    result: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
    for (label, channel), part in group.groupby([dim_col, "channel"], dropna=False):
        if label is None or pd.isna(label) or not str(label).strip():
            continue
        if channel is None or pd.isna(channel) or not str(channel).strip():
            continue
        values = period_value_map(part, periods)
        result.setdefault(str(label), {})[str(channel)] = {
            period: {"raw_value": value} for period, value in values.items()
        }
    return result

def build_sku_dimension_data(group: pd.DataFrame, periods: list[str]) -> dict[str, dict[str, dict[str, dict[str, float]]]]:
    return {
        dim_col: build_dimensional_history(group, dim_col, periods)
        for dim_col in SKU_DIMENSION_COLUMNS
        if dim_col in group.columns
    }

def build_sku_dimension_channel_data(group: pd.DataFrame, periods: list[str]) -> dict[str, dict[str, dict[str, dict[str, dict[str, float]]]]]:
    return {
        dim_col: build_dimension_channel_history(group, dim_col, periods)
        for dim_col in SKU_DIMENSION_COLUMNS
        if dim_col in group.columns
    }

def build_channel_specialty_matrix(group: pd.DataFrame, periods: list[str]) -> dict[str, dict[str, dict[str, float]]]:
    result: dict[str, dict[str, dict[str, float]]] = {}
    if "channel" not in group.columns or "specialty" not in group.columns:
        return result
    for (channel, specialty), part in group.groupby(["channel", "specialty"], dropna=False):
        if pd.isna(channel) or pd.isna(specialty):
            continue
        result.setdefault(str(channel), {})[str(specialty)] = period_value_map(part, periods)
    return result

def build_products(group: pd.DataFrame, periods: list[str]) -> list[dict[str, Any]]:
    products = []
    for (product_name, product_code), part in group.groupby(["product_name", "product_code"], dropna=False):
        if pd.isna(product_name):
            continue
        history = period_value_map(part, periods)
        products.append(
            {
                "product_name": str(product_name),
                "product_code": None if pd.isna(product_code) else str(product_code),
                "raw_value_total": float(sum(history.values())),
                "raw_value_history": history,
            }
        )
    return sorted(products, key=lambda item: item["raw_value_total"], reverse=True)
