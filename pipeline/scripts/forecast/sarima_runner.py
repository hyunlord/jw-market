#!/usr/bin/env python3
"""Phase 29 SARIMAX helpers for the 리바로/헴리브라 POC."""

from __future__ import annotations

import warnings
from typing import Any

import numpy as np
import pandas as pd
import statsmodels.api as sm

from pipeline.scripts.etl.cache_build_common import decode_json, mariadb_connect, period_key, safe_float


SOURCE_TO_INTERNAL = {"UBIST": "ubist", "IQVIA": "iqvia_nsa", "ubist": "ubist", "iqvia_nsa": "iqvia_nsa"}


def get_brand_history(brand: str, source: str, measure: str = "sales") -> pd.Series:
    internal_source = SOURCE_TO_INTERNAL[source]
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT metric_history
                FROM mart_strategic_ml_brand_metric
                WHERE brand_name = %s
                  AND source = %s
                  AND measure = %s
                ORDER BY is_jw DESC, id
                LIMIT 1
                """,
                [brand, internal_source, measure],
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError(f"No history for {brand}/{source}/{measure}")
    history = decode_json(row["metric_history"]) or {}
    periods = sorted(history.keys(), key=period_key)
    values = []
    for period in periods:
        item = history[period]
        value = item.get("raw_value") if isinstance(item, dict) else item
        values.append(safe_float(value) or 0.0)
    return pd.Series(values, index=pd.Index([str(period) for period in periods], name="period"), dtype="float64")


def model_config(source: str) -> dict[str, Any]:
    if SOURCE_TO_INTERNAL[source] == "ubist":
        return {"season": 12, "forecast_steps": 12, "holdout_steps": 6, "period_unit": "month"}
    return {"season": 4, "forecast_steps": 4, "holdout_steps": 2, "period_unit": "quarter"}


def fit_sarimax(series: pd.Series, *, source: str, exog: np.ndarray | None = None) -> Any:
    cfg = model_config(source)
    season = cfg["season"]
    seasonal_order = (1, 1, 1, season) if len(series) >= (season * 3) else (0, 1, 0, season)
    order = (1, 1, 1) if len(series) >= 18 else (0, 1, 1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return sm.tsa.statespace.SARIMAX(
            series.astype(float),
            exog=exog,
            order=order,
            seasonal_order=seasonal_order,
            enforce_stationarity=False,
            enforce_invertibility=False,
        ).fit(disp=False, maxiter=200)


def forecast_baseline(brand: str, source: str, measure: str = "sales") -> dict[str, Any]:
    history = get_brand_history(brand, source, measure)
    cfg = model_config(source)
    result = fit_sarimax(history, source=source)
    forecast = result.get_forecast(steps=cfg["forecast_steps"])
    conf = forecast.conf_int()
    return {
        "brand": brand,
        "source": source,
        "measure": measure,
        "history_periods": history.index.tolist(),
        "history_values": history.tolist(),
        "forecast_steps": cfg["forecast_steps"],
        "forecast_values": [float(v) for v in forecast.predicted_mean.tolist()],
        "ci_lower": [float(v) for v in conf.iloc[:, 0].tolist()],
        "ci_upper": [float(v) for v in conf.iloc[:, 1].tolist()],
        "model": "SARIMAX",
        "order": "auto_phase29",
    }
