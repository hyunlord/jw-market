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


HORIZON_CI_LEVELS = {
    "1y": 0.95,
    "3y": 0.95,
    "5y": 0.95,
    "10y": 0.95,
    "method": "natural_accumulation_95_only",
    "note": "Phase 30.2: horizon 차등 제거, 모든 horizon 95% CI 자연 누적",
}
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
        out = []
        for _ in range(total):
            out.append(current)
            current = _next_month(current) if SOURCE_TO_INTERNAL[source] == "ubist" else _next_quarter(current)
        return out
    anchor_period = periods[-1]
    current = _next_month(anchor_period) if SOURCE_TO_INTERNAL[source] == "ubist" else _next_quarter(anchor_period)
    out = [anchor_period]
    for _ in range(total):
        out.append(current)
        current = _next_month(current) if SOURCE_TO_INTERNAL[source] == "ubist" else _next_quarter(current)
    return out


def _anchor_forecast_to_history(history_values: list[float], forecast_result: dict[str, Any]) -> dict[str, Any]:
    """Anchor forecast and CI arrays to the last observed history value.

    Model-native intervals start at the first out-of-sample forecast and already
    include residual uncertainty. The chart needs a t=0 bridge point where the
    history line, base scenario, and CI upper/lower all share the same value.
    """
    if not history_values:
        return forecast_result
    anchor_value = float(history_values[-1])
    anchored = copy.deepcopy(forecast_result)
    anchored["point_forecast"] = [anchor_value] + list(forecast_result.get("point_forecast") or [])
    ci = dict(forecast_result.get("ci") or {})
    for upper_key in ("ci_upper_95",):
        ci[upper_key] = [anchor_value] + list(ci.get(upper_key) or [])
    for lower_key in ("ci_lower_95",):
        ci[lower_key] = [anchor_value] + list(ci.get(lower_key) or [])
    anchored["ci"] = ci
    _enforce_accumulating_ci_width(anchored)
    return anchored


def future_periods_from_history(periods: list[str], source: str, steps: int | None = None) -> list[str]:
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


def _enforce_accumulating_ci_width(forecast_result: dict[str, Any]) -> None:
    """Keep anchored CI width from collapsing at later horizons.

    Native model intervals can occasionally narrow after clipping negative lower
    bounds or when a fallback model emits a nearly constant interval. The visual
    contract for Phase 30.4 is simpler: the anchor has zero width, then the CI
    envelope never shrinks as the horizon moves away from the last actual point.
    """
    ci = forecast_result.get("ci") or {}
    point = np.asarray(forecast_result.get("point_forecast") or [], dtype=float)
    upper = np.asarray(ci.get("ci_upper_95") or [], dtype=float)
    lower = np.asarray(ci.get("ci_lower_95") or [], dtype=float)
    n = min(len(point), len(upper), len(lower))
    if n == 0:
        return

    applied = False
    upper[0] = point[0]
    lower[0] = point[0]
    previous_width = 0.0
    for i in range(1, n):
        lower[i] = min(max(0.0, lower[i]), point[i])
        upper[i] = max(point[i], upper[i])
        width = max(0.0, upper[i] - lower[i])
        if width <= previous_width:
            target_width = previous_width + max(previous_width * 1e-9, 1e-6)
            if width > 0:
                upper_ratio = max(0.0, upper[i] - point[i]) / width
                lower_ratio = max(0.0, point[i] - lower[i]) / width
            else:
                upper_ratio = lower_ratio = 0.5
            new_lower = point[i] - (target_width * lower_ratio)
            if new_lower < 0:
                new_lower = 0.0
            new_upper = max(point[i], new_lower + target_width)
            lower[i] = new_lower
            upper[i] = new_upper
            width = upper[i] - lower[i]
            applied = True
        previous_width = width

    ci["ci_upper_95"] = _clip(upper)
    ci["ci_lower_95"] = _clip(lower)
    ci["ci_accumulation_guard_applied"] = bool(ci.get("ci_accumulation_guard_applied")) or applied
    ci["lower_floor_applied"] = bool(ci.get("lower_floor_applied")) or bool(np.any(lower[:n] <= 0))
    forecast_result["ci"] = ci


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


def _native_95_ci(point: np.ndarray | list[float], lower: np.ndarray | list[float], upper: np.ndarray | list[float]) -> dict[str, Any]:
    point_arr = np.asarray(point, dtype=float)
    lower_arr = np.asarray(lower, dtype=float)
    upper_arr = np.asarray(upper, dtype=float)
    lower_floor_applied = bool(np.any(lower_arr < 0))
    point_arr = np.maximum(point_arr, 0.0)
    lower_arr = np.maximum(lower_arr, 0.0)
    upper_arr = np.maximum(upper_arr, 0.0)
    lower_arr = np.minimum(lower_arr, point_arr)
    upper_arr = np.maximum(upper_arr, point_arr)
    return {
        "ci_upper_95": _clip(upper_arr),
        "ci_lower_95": _clip(lower_arr),
        "lower_floor_applied": lower_floor_applied,
    }


def _fit_prophet(periods: list[str], values: list[float], source: str, steps: int) -> tuple[list[float], dict[str, Any], float, dict[str, Any]]:
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
            uncertainty_samples=1000,
            interval_width=0.95,
        )
        model.fit(df)
        in_sample = model.predict(df)["yhat"].to_numpy(dtype=float)
        future = model.make_future_dataframe(periods=steps, freq="MS", include_history=False)
        forecast = model.predict(future)
    point = forecast["yhat"].to_numpy(dtype=float)
    ci = _native_95_ci(
        point,
        forecast["yhat_lower"].to_numpy(dtype=float),
        forecast["yhat_upper"].to_numpy(dtype=float),
    )
    return _clip(point), ci, _residual_std(np.asarray(values, dtype=float), in_sample), {
        "name": "Prophet",
        "variant": "basic_with_light_proxy_events",
        "params": {
            "seasonality_mode": "additive",
            "yearly_seasonality": True,
            "weekly_seasonality": False,
            "daily_seasonality": False,
            "uncertainty_samples": 1000,
            "interval_width": 0.95,
        },
    }


def _fit_sarimax(values: list[float], source: str, steps: int, spec: ModelSpec) -> tuple[list[float], dict[str, Any], float, dict[str, Any]]:
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
    forecast_obj = result.get_forecast(steps=steps)
    forecast = forecast_obj.predicted_mean
    ci_95 = forecast_obj.conf_int(alpha=0.05)
    fitted = np.asarray(result.fittedvalues, dtype=float)
    ci = _native_95_ci(
        np.asarray(forecast, dtype=float),
        ci_95.iloc[:, 0].to_numpy(dtype=float),
        ci_95.iloc[:, 1].to_numpy(dtype=float),
    )
    return _clip(forecast), ci, _residual_std(np.asarray(values, dtype=float), fitted), {
        "name": "SARIMAX",
        "variant": spec.variant if spec.name == "SARIMAX" else "prophet_fallback",
        "params": {"order": list(order), "seasonal_order": list(seasonal_order)},
    }


def _fit_holtwinters(values: list[float], source: str, steps: int, spec: ModelSpec) -> tuple[list[float], dict[str, Any], float, dict[str, Any]]:
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
    residual_std = _residual_std(np.asarray(values, dtype=float), fitted)
    try:
        simulations = result.simulate(
            steps,
            anchor="end",
            repetitions=1000,
            random_errors="bootstrap",
        )
        sim_arr = np.asarray(simulations, dtype=float)
        if sim_arr.ndim == 1:
            sim_arr = sim_arr.reshape(steps, 1)
        if sim_arr.shape[0] != steps and sim_arr.shape[-1] == steps:
            sim_arr = sim_arr.T
        lower = np.nanpercentile(sim_arr, 2.5, axis=1)
        upper = np.nanpercentile(sim_arr, 97.5, axis=1)
    except Exception:
        point_arr = np.asarray(forecast, dtype=float)
        growth = np.sqrt(1.0 + np.arange(steps, dtype=float) / max(season, 1))
        lower = point_arr - 1.96 * residual_std * growth
        upper = point_arr + 1.96 * residual_std * growth
    ci = _native_95_ci(np.asarray(forecast, dtype=float), lower, upper)
    return _clip(forecast), ci, residual_std, {
        "name": "HoltWinters",
        "variant": spec.variant,
        "params": {"trend": "add" if len(values) >= 4 else None, "seasonal": seasonal, "damped_trend": len(values) >= 4, "seasonal_periods": seasonal_periods},
    }


def _fit_linear(values: list[float], steps: int) -> tuple[list[float], dict[str, Any], float, dict[str, Any]]:
    x = np.arange(len(values), dtype=float)
    y = np.asarray(values, dtype=float)
    future_x = np.arange(len(values), len(values) + steps, dtype=float)
    if len(values) >= 2:
        X = sm.add_constant(x, has_constant="add")
        model = sm.OLS(y, X).fit()
        X_future = sm.add_constant(future_x, has_constant="add")
        pred = model.get_prediction(X_future)
        summary = pred.summary_frame(alpha=0.05)
        forecast = summary["mean"].to_numpy(dtype=float)
        lower = summary["obs_ci_lower"].to_numpy(dtype=float)
        upper = summary["obs_ci_upper"].to_numpy(dtype=float)
        fitted = np.asarray(model.fittedvalues, dtype=float)
        slope = float(model.params[1]) if len(model.params) > 1 else 0.0
        intercept = float(model.params[0]) if len(model.params) else (float(y[-1]) if len(y) else 0.0)
    else:
        slope, intercept = 0.0, float(y[-1]) if len(y) else 0.0
        forecast = np.full(steps, intercept)
        fitted = np.full(len(y), intercept)
        lower = forecast
        upper = forecast
    ci = _native_95_ci(forecast, lower, upper)
    return _clip(forecast), ci, _residual_std(y, fitted), {
        "name": "Linear",
        "variant": "base",
        "params": {"degree": 1, "slope": float(slope), "intercept": float(intercept)},
    }


def _fit_mean(values: list[float], steps: int) -> tuple[list[float], dict[str, Any], float, dict[str, Any]]:
    y = np.asarray(values, dtype=float)
    mean_value = float(np.nanmean(y)) if len(y) else 0.0
    fitted = np.full(len(y), mean_value)
    std = float(np.nanstd(y)) if len(y) else 0.0
    point = np.full(steps, max(0.0, mean_value))
    ci = _native_95_ci(point, point - (1.96 * std), point + (1.96 * std))
    return _clip(point), ci, _residual_std(y, fitted), {
        "name": "Mean",
        "variant": "base",
        "params": {"window": "all"},
    }


def _fit_values(periods: list[str], values: list[float], source: str, steps: int) -> dict[str, Any]:
    spec = select_model(len(values), source)
    warnings_list: list[str] = []
    if not values:
        point, ci, residual_std, actual_model = _fit_mean([0.0], steps)
        warnings_list.append("no_history_mean_fallback")
    else:
        try:
            if spec.name == "Prophet":
                point, ci, residual_std, actual_model = _fit_prophet(periods, values, source, steps)
            elif spec.name == "SARIMAX":
                point, ci, residual_std, actual_model = _fit_sarimax(values, source, steps, spec)
            elif spec.name == "HoltWinters":
                point, ci, residual_std, actual_model = _fit_holtwinters(values, source, steps, spec)
            elif spec.name == "Linear":
                point, ci, residual_std, actual_model = _fit_linear(values, steps)
            else:
                point, ci, residual_std, actual_model = _fit_mean(values, steps)
        except Exception as exc:
            warnings_list.append(f"{spec.name.lower()}_fit_failed_fallback:{type(exc).__name__}")
            try:
                if len(values) >= 20:
                    point, ci, residual_std, actual_model = _fit_holtwinters(values, source, steps, select_model(20, source))
                elif len(values) >= 12:
                    point, ci, residual_std, actual_model = _fit_linear(values, steps)
                else:
                    point, ci, residual_std, actual_model = _fit_mean(values, steps)
            except Exception as fallback_exc:
                warnings_list.append(f"fallback_fit_failed_mean:{type(fallback_exc).__name__}")
                point, ci, residual_std, actual_model = _fit_mean(values or [0.0], steps)

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
    return {
        "upper_values": list(forecast_result["ci"]["ci_upper_95"]),
        "lower_values": list(forecast_result["ci"]["ci_lower_95"]),
    }


def calculate_confidence(forecast_result: dict[str, Any], baseline_value: float | None, source: str) -> dict[str, Any]:
    steps_year = steps_per_year(source)
    idx = min(steps_year, len(forecast_result["point_forecast"]) - 1)
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
    if brand_forecast and market_forecast and brand_start == brand_forecast[0] and market_start == market_forecast[0]:
        n = max(0, n - 1)
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


def build_forecast_result(periods: list[str], values: list[float], source: str, steps: int | None = None) -> dict[str, Any]:
    steps = steps if steps is not None else forecast_steps(source)
    result = _anchor_forecast_to_history(values, _fit_values(periods, values, source, steps))
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
            "upper_95_natural": forecast_result["adaptive_ci"]["upper_values"],
            "lower_95_natural": forecast_result["adaptive_ci"]["lower_values"],
            "lower_floor_applied": bool(forecast_result["ci"].get("lower_floor_applied")),
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
        upper_values = intervals.get("upper_95_natural") or intervals.get("upper_horizon_adaptive") or base_values
        lower_values = intervals.get("lower_95_natural") or intervals.get("lower_horizon_adaptive") or base_values
        final_base = base_values[-1] if base_values else None
        final_upper = upper_values[-1] if upper_values else None
        final_lower = lower_values[-1] if lower_values else None
        floor_lower = bool(intervals.get("lower_floor_applied"))
        market_comparison = calculate_market_comparison(
            entry.get("history_values") or [],
            base_values,
            market_forecast.get("history_values") or [],
            market_forecast.get("forecast_values") or [],
            source,
        )
        warnings_list = list(entry.get("forecast_warnings") or [])
        warnings_list.extend(["event_regressor_disabled_phase_30", "forecast_horizon_10y_is_extrapolation_heavy"])
        if floor_lower:
            warnings_list.append("floor_applied_declining_trend")
        by_brand[brand_name] = {
            "target_period": forecast_combo.get("forecast_periods", [None])[-1] if forecast_combo.get("forecast_periods") else None,
            "history_periods": entry.get("history_periods") or [],
            "forecast_periods": forecast_combo.get("forecast_periods") or [],
            "history_values": entry.get("history_values") or [],
            "forecast_values": base_values,
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
                    "method": "selected_model_ci_upper_95_natural",
                    "values": upper_values,
                    "final_value": final_upper,
                    "delta_pct_vs_base": ((final_upper - final_base) / final_base * 100) if final_base and final_upper is not None else None,
                },
                "lower": {
                    "label": "하위 (Worst)",
                    "method": "selected_model_ci_lower_95_natural",
                    "values": lower_values,
                    "final_value": final_lower,
                    "delta_pct_vs_base": ((final_lower - final_base) / final_base * 100) if final_base and final_lower is not None else None,
                    "floor_applied": floor_lower,
                },
            },
            "confidence": entry.get("confidence"),
            "market_comparison": market_comparison,
            "momentum": calculate_momentum(base_values, source),
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
