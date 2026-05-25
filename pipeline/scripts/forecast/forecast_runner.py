#!/usr/bin/env python3
"""Phase 30 forecast runner for deep-analysis forecast/simulation payloads.

The runner is intentionally dependency-light at call sites: Prophet is used
when available for UBIST 60+ monthly histories, and all other cases dispatch
to statsmodels / linear / mean fallbacks according to data_size_dispatch_v1.
Event regressors stay disabled in Phase 30.
"""

from __future__ import annotations

import argparse
import copy
from collections import defaultdict
from dataclasses import dataclass
import json
import logging
import math
from pathlib import Path
import re
from typing import Any
import warnings

import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.tsa.holtwinters import ExponentialSmoothing

from pipeline.scripts.etl.cache_build_common import (
    CANONICAL_25,
    decode_json,
    fetch_all,
    metric_recent,
    period_key,
    safe_float,
)


HORIZON_CI_LEVELS = {"1y": 0.95, "3y": 0.90, "5y": 0.80, "10y": 0.50}
Z_BY_LEVEL = {0.95: 1.96, 0.90: 1.645, 0.80: 1.282, 0.50: 0.674}
SOURCE_TO_INTERNAL = {"UBIST": "ubist", "IQVIA": "iqvia_nsa", "ubist": "ubist", "iqvia_nsa": "iqvia_nsa"}
UNIT_LABELS = {
    "sales": "KRW",
    "volume": "Rx",
    "unit": "unit",
    "dosage_unit": "dosage unit",
    "counting_unit": "counting unit",
}

_FORECAST_ENTRY_CACHE: dict[tuple[Any, str, str, int], dict[str, Any]] = {}
_MARKET_FORECAST_CACHE: dict[tuple[tuple[Any, ...], str, int], dict[str, Any]] = {}


@dataclass(frozen=True)
class ModelSpec:
    name: str
    variant: str
    params: dict[str, Any]
    selection_reason: str
    selection_policy: str = "data_size_dispatch_v1"


def select_model(n_history: int, source: str) -> ModelSpec:
    source = "UBIST" if SOURCE_TO_INTERNAL[source] == "ubist" else "IQVIA"
    if source == "UBIST":
        if n_history >= 60:
            return ModelSpec(
                "Prophet",
                "basic_with_light_proxy_events",
                {"seasonality_mode": "additive", "yearly_seasonality": True, "weekly_seasonality": False, "daily_seasonality": False},
                f"data_size_{n_history}_months_supports_prophet_basic",
            )
        if n_history >= 40:
            return ModelSpec(
                "SARIMAX",
                "event_1_2",
                {"order": [1, 1, 1], "seasonal_order": [1, 1, 1, 12]},
                f"data_size_{n_history}_months_supports_sarimax_event_proxy_disabled",
            )
        if n_history >= 30:
            return ModelSpec(
                "SARIMAX",
                "base",
                {"order": [1, 1, 1], "seasonal_order": [1, 1, 1, 12]},
                f"data_size_{n_history}_months_supports_sarimax_base",
            )
        if n_history >= 20:
            return ModelSpec(
                "HoltWinters",
                "damped",
                {"trend": "add", "seasonal": "add", "damped_trend": True, "seasonal_periods": 12},
                f"data_size_{n_history}_months_supports_holtwinters_damped",
            )
        if n_history >= 12:
            return ModelSpec("Linear", "base", {"degree": 1}, f"data_size_{n_history}_months_supports_linear")
        return ModelSpec("Mean", "base", {"window": "all"}, f"data_size_{n_history}_months_supports_mean")

    if n_history >= 20:
        return ModelSpec(
            "HoltWinters",
            "damped",
            {"trend": "add", "seasonal": "add", "damped_trend": True, "seasonal_periods": 4},
            f"data_size_{n_history}_quarters_supports_holtwinters_damped",
        )
    if n_history >= 12:
        return ModelSpec("Linear", "base", {"degree": 1}, f"data_size_{n_history}_quarters_supports_linear")
    return ModelSpec("Mean", "base", {"window": "all"}, f"data_size_{n_history}_quarters_supports_mean")


def steps_per_year(source: str) -> int:
    return 12 if SOURCE_TO_INTERNAL[source] == "ubist" else 4


def forecast_steps(source: str) -> int:
    return steps_per_year(source) * 10


def period_unit(source: str) -> str:
    return "월" if SOURCE_TO_INTERNAL[source] == "ubist" else "분기"


def _next_month(period: str) -> str:
    year, month = map(int, period.split("-"))
    month += 1
    if month > 12:
        year += 1
        month = 1
    return f"{year:04d}-{month:02d}"


def _next_quarter(period: str) -> str:
    match = re.match(r"^(\d{4})-?Q([1-4])$", str(period))
    if not match:
        return "2026-Q1"
    year = int(match.group(1))
    quarter = int(match.group(2)) + 1
    if quarter > 4:
        year += 1
        quarter = 1
    return f"{year:04d}-Q{quarter}"


def forecast_periods_from_history(periods: list[str], source: str, steps: int | None = None) -> list[str]:
    total = steps if steps is not None else forecast_steps(source)
    if not periods:
        current = "2026-05" if SOURCE_TO_INTERNAL[source] == "ubist" else "2026-Q1"
    else:
        current = _next_month(periods[-1]) if SOURCE_TO_INTERNAL[source] == "ubist" else _next_quarter(periods[-1])
    out = []
    for _ in range(total):
        out.append(current)
        current = _next_month(current) if SOURCE_TO_INTERNAL[source] == "ubist" else _next_quarter(current)
    return out


def history_from_row(row: dict[str, Any]) -> tuple[list[str], list[float]]:
    history = decode_json(row.get("metric_history")) or {}
    periods = sorted((history or {}).keys(), key=period_key)
    values: list[float] = []
    for period in periods:
        item = history.get(period)
        raw = item.get("raw_value") if isinstance(item, dict) else item
        values.append(safe_float(raw) or 0.0)
    return [str(period) for period in periods], values


def aggregate_market_history(rows: list[dict[str, Any]]) -> tuple[list[str], list[float]]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        periods, values = history_from_row(row)
        for period, value in zip(periods, values):
            totals[period] += value
    periods = sorted(totals.keys(), key=period_key)
    return periods, [totals[period] for period in periods]


def _clip(values: np.ndarray | list[float]) -> list[float]:
    return [float(max(0.0, value)) for value in np.asarray(values, dtype=float).tolist()]


def _residual_std(actual: np.ndarray, fitted: np.ndarray) -> float:
    if len(actual) == 0:
        return 0.0
    residuals = actual - fitted[: len(actual)]
    std = float(np.nanstd(residuals))
    level = float(np.nanmean(np.abs(actual))) if len(actual) else 0.0
    return max(std, level * 0.03, 1.0)


def _ci_arrays(point: list[float], residual_std: float, source: str) -> dict[str, list[float]]:
    season = steps_per_year(source)
    point_arr = np.asarray(point, dtype=float)
    growth = np.sqrt(1.0 + (np.arange(len(point_arr), dtype=float) / max(season, 1)))
    out: dict[str, list[float]] = {}
    for level, z in Z_BY_LEVEL.items():
        width = z * residual_std * growth
        suffix = str(int(level * 100))
        out[f"ci_upper_{suffix}"] = _clip(point_arr + width)
        out[f"ci_lower_{suffix}"] = _clip(point_arr - width)
    return out


def _fit_prophet(periods: list[str], values: list[float], source: str, steps: int) -> tuple[list[float], float, dict[str, Any]]:
    if SOURCE_TO_INTERNAL[source] != "ubist":
        raise RuntimeError("Prophet path is monthly-only in Phase 30")
    from prophet import Prophet

    logging.getLogger("cmdstanpy").setLevel(logging.WARNING)
    logging.getLogger("prophet").setLevel(logging.WARNING)
    dates = pd.to_datetime([f"{period}-01" for period in periods])
    df = pd.DataFrame({"ds": dates, "y": values})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = Prophet(
            seasonality_mode="additive",
            yearly_seasonality=True,
            weekly_seasonality=False,
            daily_seasonality=False,
            uncertainty_samples=0,
        )
        model.fit(df)
        in_sample = model.predict(df)["yhat"].to_numpy(dtype=float)
        future = model.make_future_dataframe(periods=steps, freq="MS", include_history=False)
        forecast = model.predict(future)["yhat"].to_numpy(dtype=float)
    return _clip(forecast), _residual_std(np.asarray(values, dtype=float), in_sample), {
        "name": "Prophet",
        "variant": "basic_with_light_proxy_events",
        "params": {"seasonality_mode": "additive", "yearly_seasonality": True, "weekly_seasonality": False, "daily_seasonality": False},
    }


def _fit_sarimax(values: list[float], source: str, steps: int, spec: ModelSpec) -> tuple[list[float], float, dict[str, Any]]:
    season = steps_per_year(source)
    seasonal_order = (1, 1, 1, season) if len(values) >= season * 3 else (0, 1, 0, season)
    order = (1, 1, 1) if len(values) >= 18 else (0, 1, 1)
    series = pd.Series(values, dtype="float64")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = sm.tsa.statespace.SARIMAX(
            series,
            order=order,
            seasonal_order=seasonal_order,
            enforce_stationarity=False,
            enforce_invertibility=False,
        ).fit(disp=False, maxiter=120)
    forecast = result.forecast(steps=steps)
    fitted = np.asarray(result.fittedvalues, dtype=float)
    return _clip(forecast), _residual_std(np.asarray(values, dtype=float), fitted), {
        "name": "SARIMAX",
        "variant": spec.variant if spec.name == "SARIMAX" else "prophet_fallback",
        "params": {"order": list(order), "seasonal_order": list(seasonal_order)},
    }


def _fit_holtwinters(values: list[float], source: str, steps: int, spec: ModelSpec) -> tuple[list[float], float, dict[str, Any]]:
    season = steps_per_year(source)
    seasonal = "add" if len(values) >= season * 2 else None
    seasonal_periods = season if seasonal else None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = ExponentialSmoothing(
            values,
            trend="add" if len(values) >= 4 else None,
            damped_trend=len(values) >= 4,
            seasonal=seasonal,
            seasonal_periods=seasonal_periods,
            initialization_method="estimated",
        )
        result = model.fit(optimized=True)
    forecast = result.forecast(steps)
    fitted = np.asarray(result.fittedvalues, dtype=float)
    return _clip(forecast), _residual_std(np.asarray(values, dtype=float), fitted), {
        "name": "HoltWinters",
        "variant": spec.variant,
        "params": {"trend": "add" if len(values) >= 4 else None, "seasonal": seasonal, "damped_trend": len(values) >= 4, "seasonal_periods": seasonal_periods},
    }


def _fit_linear(values: list[float], steps: int) -> tuple[list[float], float, dict[str, Any]]:
    x = np.arange(len(values), dtype=float)
    y = np.asarray(values, dtype=float)
    if len(values) >= 2:
        slope, intercept = np.polyfit(x, y, 1)
    else:
        slope, intercept = 0.0, y[-1] if len(y) else 0.0
    future_x = np.arange(len(values), len(values) + steps, dtype=float)
    forecast = intercept + slope * future_x
    fitted = intercept + slope * x
    return _clip(forecast), _residual_std(y, fitted), {
        "name": "Linear",
        "variant": "base",
        "params": {"degree": 1, "slope": float(slope), "intercept": float(intercept)},
    }


def _fit_mean(values: list[float], steps: int) -> tuple[list[float], float, dict[str, Any]]:
    y = np.asarray(values, dtype=float)
    mean_value = float(np.nanmean(y)) if len(y) else 0.0
    fitted = np.full(len(y), mean_value)
    return [max(0.0, mean_value)] * steps, _residual_std(y, fitted), {
        "name": "Mean",
        "variant": "base",
        "params": {"window": "all"},
    }


def _fit_values(periods: list[str], values: list[float], source: str, steps: int) -> dict[str, Any]:
    spec = select_model(len(values), source)
    warnings_list: list[str] = []
    if not values:
        point, residual_std, actual_model = _fit_mean([0.0], steps)
        warnings_list.append("no_history_mean_fallback")
    else:
        try:
            if spec.name == "Prophet":
                point, residual_std, actual_model = _fit_prophet(periods, values, source, steps)
            elif spec.name == "SARIMAX":
                point, residual_std, actual_model = _fit_sarimax(values, source, steps, spec)
            elif spec.name == "HoltWinters":
                point, residual_std, actual_model = _fit_holtwinters(values, source, steps, spec)
            elif spec.name == "Linear":
                point, residual_std, actual_model = _fit_linear(values, steps)
            else:
                point, residual_std, actual_model = _fit_mean(values, steps)
        except Exception as exc:
            warnings_list.append(f"{spec.name.lower()}_fit_failed_fallback:{type(exc).__name__}")
            try:
                if len(values) >= 20:
                    point, residual_std, actual_model = _fit_holtwinters(values, source, steps, select_model(20, source))
                elif len(values) >= 12:
                    point, residual_std, actual_model = _fit_linear(values, steps)
                else:
                    point, residual_std, actual_model = _fit_mean(values, steps)
            except Exception as fallback_exc:
                warnings_list.append(f"fallback_fit_failed_mean:{type(fallback_exc).__name__}")
                point, residual_std, actual_model = _fit_mean(values or [0.0], steps)

    ci = _ci_arrays(point, residual_std, source)
    fit_quality = _fit_quality(values, source)
    return {
        "point_forecast": point,
        "residual_std": residual_std,
        "dispatch_spec": spec,
        "actual_model": actual_model,
        "ci": ci,
        "fit_quality": fit_quality,
        "warnings": warnings_list,
    }


def _fit_quality(values: list[float], source: str) -> dict[str, Any]:
    holdout = min(3, max(1, len(values) // 5))
    if len(values) <= holdout + 4:
        return {"mape_backtest_3m": None, "residual_std": None, "backtest_available": False}
    train = values[:-holdout]
    actual = np.asarray(values[-holdout:], dtype=float)
    season = steps_per_year(source)
    preds = []
    for i in range(holdout):
        if len(train) >= season:
            preds.append(train[-season + (i % season)])
        else:
            preds.append(train[-1])
    pred_arr = np.asarray(preds, dtype=float)
    denom = np.where(actual == 0, np.nan, actual)
    mape = float(np.nanmean(np.abs((actual - pred_arr) / denom)) * 100) if not np.all(np.isnan(denom)) else None
    return {
        "mape_backtest_3m": mape,
        "residual_std": float(np.nanstd(actual - pred_arr)),
        "backtest_available": mape is not None,
    }


def build_horizon_adaptive_ci(forecast_result: dict[str, Any], source: str) -> dict[str, list[float]]:
    steps_year = steps_per_year(source)
    total = len(forecast_result["point_forecast"])
    upper: list[float] = []
    lower: list[float] = []
    for idx in range(total):
        if idx < steps_year:
            suffix = "95"
        elif idx < steps_year * 3:
            suffix = "90"
        elif idx < steps_year * 5:
            suffix = "80"
        else:
            suffix = "50"
        upper.append(forecast_result["ci"][f"ci_upper_{suffix}"][idx])
        lower.append(forecast_result["ci"][f"ci_lower_{suffix}"][idx])
    return {"upper_values": upper, "lower_values": lower}


def calculate_confidence(forecast_result: dict[str, Any], baseline_value: float | None, source: str) -> dict[str, Any]:
    steps_year = steps_per_year(source)
    idx = min(steps_year - 1, len(forecast_result["point_forecast"]) - 1)
    upper = forecast_result["ci"]["ci_upper_95"][idx] if idx >= 0 else 0.0
    lower = forecast_result["ci"]["ci_lower_95"][idx] if idx >= 0 else 0.0
    ci_width_absolute = max(0.0, upper - lower)
    base = baseline_value if baseline_value and baseline_value > 0 else max(forecast_result["point_forecast"][idx] if idx >= 0 else 0.0, 1.0)
    ci_width_relative_pct = ci_width_absolute / base * 100
    if ci_width_relative_pct < 10:
        score, label = 95, "매우높음"
    elif ci_width_relative_pct < 25:
        score, label = 80, "높음"
    elif ci_width_relative_pct < 50:
        score, label = 65, "보통"
    elif ci_width_relative_pct < 100:
        score, label = 40, "낮음"
    else:
        score, label = 20, "매우낮음"
    return {
        "score": score,
        "method": "ci_width_normalized",
        "ci_width_absolute": ci_width_absolute,
        "ci_width_relative_pct": ci_width_relative_pct,
        "label": label,
    }


def calculate_momentum(forecast_values: list[float], source: str, n_periods: int = 3) -> dict[str, Any]:
    if len(forecast_values) < 2:
        avg = 0.0
    else:
        limit = min(n_periods, len(forecast_values))
        deltas = []
        for idx in range(1, limit):
            prev = forecast_values[idx - 1]
            cur = forecast_values[idx]
            deltas.append((cur - prev) / prev * 100 if prev else 0.0)
        avg = sum(deltas) / len(deltas) if deltas else 0.0
    if avg > 1.0:
        label = "가속 추세"
    elif avg > 0:
        label = "안정 성장"
    elif avg > -1.0:
        label = "감속"
    else:
        label = "하향 추세"
    return {
        "value_pct_per_period": avg,
        "label": label,
        "basis": "forecast_first_n_periods",
        "n_periods": n_periods,
        "method": "forecast_slope_avg",
        "interpretation": f"{period_unit(source)} forecast first {n_periods} period slope",
    }


def _cagr(start: float | None, end: float | None, periods: int, source: str) -> float | None:
    if start is None or end is None or start <= 0 or end <= 0 or periods <= 0:
        return None
    years = periods / steps_per_year(source)
    if years <= 0:
        return None
    return ((end / start) ** (1 / years) - 1) * 100


def calculate_market_comparison(
    brand_history: list[float],
    brand_forecast: list[float],
    market_history: list[float],
    market_forecast: list[float],
    source: str,
) -> dict[str, Any]:
    brand_start = brand_history[-1] if brand_history else None
    brand_end = brand_forecast[-1] if brand_forecast else None
    market_start = market_history[-1] if market_history else None
    market_end = market_forecast[-1] if market_forecast else None
    n = min(len(brand_forecast), len(market_forecast))
    brand_cagr = _cagr(brand_start, brand_end, n, source)
    market_cagr = _cagr(market_start, market_end, n, source)
    delta = (brand_cagr - market_cagr) if brand_cagr is not None and market_cagr is not None else None
    return {
        "delta_pp": delta,
        "brand_cagr_pct": brand_cagr,
        "market_cagr_pct": market_cagr,
        "basis": "same_atc4_within_source",
        "horizon": "forecast_period",
        "method": "brand_cagr_minus_market_cagr_same_source",
    }


def detect_anomalies(history_values: list[float], history_periods: list[str], source: str, cut_b_events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    window = 6 if SOURCE_TO_INTERNAL[source] == "ubist" else 4
    season = steps_per_year(source)
    threshold_z = 3.0
    threshold_yoy = 50.0
    cut_b_events = cut_b_events or []
    items: list[dict[str, Any]] = []
    scored: list[tuple[float, dict[str, Any]]] = []
    for idx in range(window, len(history_values)):
        rolling = np.asarray(history_values[idx - window : idx], dtype=float)
        expected = float(np.mean(rolling)) if len(rolling) else 0.0
        std = float(np.std(rolling)) or 0.0
        value = float(history_values[idx])
        z = (value - expected) / std if std else 0.0
        yoy = None
        if idx >= season and history_values[idx - season]:
            yoy = (value - history_values[idx - season]) / history_values[idx - season] * 100
        delta_pct = (value - expected) / expected * 100 if expected else 0.0
        threshold_pass = abs(z) >= threshold_z or (yoy is not None and abs(yoy) >= threshold_yoy)
        item = {
            "period": history_periods[idx],
            "value": value,
            "expected_value": expected,
            "delta_pct": delta_pct,
            "yoy_pct": yoy,
            "z_score": z,
            "direction": "up" if z >= 0 else "down",
            "threshold_pass": threshold_pass,
            "fallback_rank": None,
            "matched_event_id": _match_event_id(history_periods[idx], source, cut_b_events),
        }
        scored.append((abs(z) + (abs(yoy) / 100 if yoy is not None else 0.0), item))
        if threshold_pass:
            items.append(item)
    if len(items) < 3:
        existing = {item["period"] for item in items}
        for rank, (_, item) in enumerate(sorted(scored, key=lambda pair: pair[0], reverse=True), 1):
            if item["period"] in existing:
                continue
            item = dict(item)
            item["fallback_rank"] = rank
            item["threshold_pass"] = False
            items.append(item)
            existing.add(item["period"])
            if len(items) >= 3:
                break
    return {
        "method": "rolling_z_score_with_yoy_check",
        "threshold_z": threshold_z,
        "threshold_yoy_pct": threshold_yoy,
        "window": window,
        "fallback_top_n": 3,
        "items": items[:8],
    }


def _match_event_id(period: str, source: str, cut_b_events: list[dict[str, Any]]) -> str | None:
    key = "UBIST" if SOURCE_TO_INTERNAL[source] == "ubist" else "IQVIA"
    for event in cut_b_events:
        if (event.get("period_map") or {}).get(key) == period:
            return event.get("id") or event.get("event_id") or event.get("news_id")
    return None


def build_forecast_result(periods: list[str], values: list[float], source: str, steps: int | None = None) -> dict[str, Any]:
    steps = steps if steps is not None else forecast_steps(source)
    result = _fit_values(periods, values, source, steps)
    adaptive = build_horizon_adaptive_ci(result, source)
    result["adaptive_ci"] = adaptive
    return result


def build_forecast_brand_entry(
    brand_row: dict[str, Any],
    *,
    target_brand: str,
    source: str,
    measure: str,
    forecast_steps_count: int,
) -> dict[str, Any]:
    cache_key = (brand_row.get("id") or brand_row.get("brand_key") or brand_row.get("brand_name"), source, measure, forecast_steps_count)
    if cache_key in _FORECAST_ENTRY_CACHE:
        cached = copy.deepcopy(_FORECAST_ENTRY_CACHE[cache_key])
        cached["is_target"] = cached.get("brand") == target_brand
        return cached
    periods, values = history_from_row(brand_row)
    forecast_result = build_forecast_result(periods, values, source, forecast_steps_count)
    recent = metric_recent(decode_json(brand_row.get("metric_history")))
    baseline_value = safe_float(recent.get("raw_value"))
    actual_model = forecast_result["actual_model"]
    dispatch_spec: ModelSpec = forecast_result["dispatch_spec"]
    model_payload = {
        "name": actual_model["name"],
        "variant": actual_model["variant"],
        "params": actual_model["params"],
        "selection_reason": dispatch_spec.selection_reason if actual_model["name"] == dispatch_spec.name else f"{dispatch_spec.name.lower()}_fallback_to_{actual_model['name'].lower()}",
        "selection_policy": dispatch_spec.selection_policy,
        "event_regressor": {
            "enabled": False,
            "mode": "proxy_light",
            "max_regressors": 0,
            "regressors": [],
            "limitations": ["event_regressor_disabled_phase_30", "phase_30_baseline_only_no_sentiment"],
        },
        "fit_quality": forecast_result["fit_quality"],
    }
    entry = {
        "brand": brand_row.get("brand_name"),
        "company": None,
        "is_target": brand_row.get("brand_name") == target_brand,
        "is_jw": bool(brand_row.get("is_jw")),
        "rank": recent.get("rank"),
        "history_periods": periods,
        "history_values": values,
        "forecast_values": forecast_result["point_forecast"],
        "forecast_model": model_payload,
        "forecast_intervals": {
            "upper_horizon_adaptive": forecast_result["adaptive_ci"]["upper_values"],
            "lower_horizon_adaptive": forecast_result["adaptive_ci"]["lower_values"],
            **forecast_result["ci"],
        },
        "forecast_warnings": forecast_result["warnings"],
        "confidence": calculate_confidence(forecast_result, baseline_value, source),
        "baseline": {"value_recent": baseline_value, "ms_recent_pct": safe_float(recent.get("ms"))},
    }
    _FORECAST_ENTRY_CACHE[cache_key] = copy.deepcopy(entry)
    return entry


def build_market_forecast(market_rows: list[dict[str, Any]], source: str, steps: int) -> dict[str, Any]:
    row_ids = tuple(sorted(row.get("id") or row.get("brand_key") or row.get("brand_name") for row in market_rows))
    cache_key = (row_ids, source, steps)
    if cache_key in _MARKET_FORECAST_CACHE:
        return copy.deepcopy(_MARKET_FORECAST_CACHE[cache_key])
    periods, values = aggregate_market_history(market_rows)
    result = build_forecast_result(periods, values, source, steps)
    forecast = {"history_periods": periods, "history_values": values, "forecast_values": result["point_forecast"]}
    _MARKET_FORECAST_CACHE[cache_key] = copy.deepcopy(forecast)
    return forecast


def build_simulation_combo(
    *,
    combo: str,
    source: str,
    measure: str,
    unit_label: str | None,
    forecast_combo: dict[str, Any],
    market_forecast: dict[str, Any],
    cut_b_events: list[dict[str, Any]],
) -> dict[str, Any]:
    available_brands = [
        {"brand": entry["brand"], "is_target": bool(entry.get("is_target")), "is_jw": bool(entry.get("is_jw"))}
        for entry in forecast_combo.get("brands", [])
    ]
    by_brand: dict[str, Any] = {}
    for entry in forecast_combo.get("brands", []):
        brand_name = entry["brand"]
        base_values = entry.get("forecast_values") or []
        intervals = entry.get("forecast_intervals") or {}
        upper_values = intervals.get("upper_horizon_adaptive") or base_values
        lower_values = intervals.get("lower_horizon_adaptive") or base_values
        final_base = base_values[-1] if base_values else None
        final_upper = upper_values[-1] if upper_values else None
        final_lower = lower_values[-1] if lower_values else None
        floor_lower = any(float(v) <= 0 for v in lower_values)
        market_comparison = calculate_market_comparison(
            entry.get("history_values") or [],
            base_values,
            market_forecast.get("history_values") or [],
            market_forecast.get("forecast_values") or [],
            source,
        )
        anomaly = detect_anomalies(entry.get("history_values") or [], entry.get("history_periods") or [], source, cut_b_events)
        warnings_list = list(entry.get("forecast_warnings") or [])
        warnings_list.extend(["event_regressor_disabled_phase_30", "forecast_horizon_10y_is_extrapolation_heavy"])
        if floor_lower:
            warnings_list.append("floor_applied_declining_trend")
        by_brand[brand_name] = {
            "target_period": forecast_combo.get("forecast_periods", [None])[-1] if forecast_combo.get("forecast_periods") else None,
            "history_periods": entry.get("history_periods") or [],
            "forecast_periods": forecast_combo.get("forecast_periods") or [],
            "history_values": entry.get("history_values") or [],
            "model": entry.get("forecast_model"),
            "horizon_ci_levels": HORIZON_CI_LEVELS,
            "scenarios": {
                "base": {
                    "label": "기준",
                    "method": "selected_model_point_forecast",
                    "values": base_values,
                    "final_value": final_base,
                    "floor_applied": False,
                },
                "upper": {
                    "label": "상위 (Best)",
                    "method": "selected_model_ci_upper_horizon_adaptive",
                    "values": upper_values,
                    "final_value": final_upper,
                    "delta_pct_vs_base": ((final_upper - final_base) / final_base * 100) if final_base and final_upper is not None else None,
                },
                "lower": {
                    "label": "하위 (Worst)",
                    "method": "selected_model_ci_lower_horizon_adaptive",
                    "values": lower_values,
                    "final_value": final_lower,
                    "delta_pct_vs_base": ((final_lower - final_base) / final_base * 100) if final_base and final_lower is not None else None,
                    "floor_applied": floor_lower,
                },
            },
            "stress": _build_stress(anomaly),
            "confidence": entry.get("confidence"),
            "market_comparison": market_comparison,
            "momentum": calculate_momentum(base_values, source),
            "anomaly_signals": anomaly,
            "warnings": sorted(set(warnings_list)),
            "baseline": entry.get("baseline") or {"value_recent": None, "ms_recent_pct": None},
        }
    return {
        "phase30_baseline": True,
        "combo": combo,
        "period_unit": period_unit(source),
        "unit_label": unit_label or UNIT_LABELS.get(measure),
        "target_brand": forecast_combo.get("target_brand"),
        "available_brands": available_brands,
        "by_brand": by_brand,
    }


def _build_stress(anomaly: dict[str, Any]) -> dict[str, Any]:
    deltas = [safe_float(item.get("delta_pct")) or 0.0 for item in anomaly.get("items", [])]
    upper = max([delta for delta in deltas if delta > 0], default=0.0)
    lower = min([delta for delta in deltas if delta < 0], default=0.0)
    return {
        "method": "anomaly_mean_shock_reference",
        "upper_delta_pct": upper,
        "lower_delta_pct": lower,
        "upper_used_fallback": upper == 0.0,
        "lower_used_fallback": lower == 0.0,
        "shown_as_primary_ci": False,
        "note": "Stress 참고값은 rolling anomaly delta 기반이며 primary CI는 horizon adaptive interval입니다.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all-brands", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    rows = fetch_all("SELECT * FROM mart_strategic_ml_brand_metric")
    report = {"phase": "30", "brands": {}, "row_count": len(rows)}
    for row in rows:
        brand = row.get("brand_name")
        if args.all_brands and brand not in CANONICAL_25:
            continue
        source = "UBIST" if row.get("source") == "ubist" else "IQVIA"
        periods, values = history_from_row(row)
        spec = select_model(len(values), source)
        report["brands"].setdefault(brand, {})[f"{source}.{row.get('measure')}"] = {
            "history_points": len(values),
            "selected_model": spec.name,
            "variant": spec.variant,
        }
    if args.output:
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
