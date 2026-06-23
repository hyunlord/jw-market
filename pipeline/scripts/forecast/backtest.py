#!/usr/bin/env python3
"""Phase 29 SARIMAX backtest POC.

The POC is intentionally limited to:
* 리바로 / UBIST / sales
* 헴리브라 / IQVIA / sales

It compares SARIMAX baseline with SARIMAX + event sentiment exogenous input.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pipeline.scripts.etl.phase29_events import connect, ensure_events_raw_table, get_brand_events_cut_b
from pipeline.scripts.forecast.sarima_runner import fit_sarimax, get_brand_history, model_config
from pipeline.scripts.forecast.sentiment_scorer import score_events


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "forecast" / "phase29_poc_backtest.json"
POC_BRANDS = {
    "리바로": {"source": "UBIST", "measure": "sales", "combo": "UBIST.sales"},
    "헴리브라": {"source": "IQVIA", "measure": "sales", "combo": "IQVIA.sales"},
}


def _event_period_for_source(event: dict[str, Any], source: str) -> str | None:
    key = "UBIST" if source == "UBIST" else "IQVIA"
    return (event.get("period_map") or {}).get(key)


def build_exogenous(sentiments: list[dict[str, Any]], periods: list[str], *, source: str) -> np.ndarray:
    x = np.zeros(len(periods), dtype=float)
    period_to_idx = {period: idx for idx, period in enumerate(periods)}
    for sentiment in sentiments:
        event = sentiment.get("event") or {}
        event_period = _event_period_for_source(event, source)
        if event_period not in period_to_idx:
            continue
        start_idx = period_to_idx[event_period]
        score = float(sentiment.get("sentiment_score") or 0)
        duration = max(float(sentiment.get("duration_months") or 1), 1.0)
        for idx in range(start_idx, len(periods)):
            d = idx - start_idx
            x[idx] += score * math.exp(-d / duration)
    return x


def metrics(actual: pd.Series, pred: pd.Series | np.ndarray | list[float]) -> dict[str, float]:
    actual_arr = np.asarray(actual, dtype=float)
    pred_arr = np.asarray(pred, dtype=float)
    pred_arr = np.maximum(pred_arr, 0.0)
    err = actual_arr - pred_arr
    rmse = float(math.sqrt(np.mean(err**2)))
    mae = float(np.mean(np.abs(err)))
    denom = np.where(actual_arr == 0, np.nan, actual_arr)
    mape = float(np.nanmean(np.abs(err / denom)) * 100) if not np.all(np.isnan(denom)) else 0.0
    if len(actual_arr) > 1:
        actual_dir = np.diff(actual_arr) >= 0
        pred_dir = np.diff(pred_arr) >= 0
        direction_acc = float(np.mean(actual_dir == pred_dir))
    else:
        direction_acc = 1.0
    return {"rmse": rmse, "mape": mape, "mae": mae, "direction_acc": direction_acc}


def _forecast_with_optional_exog(train: pd.Series, *, source: str, steps: int, exog_train: np.ndarray | None, exog_test: np.ndarray | None) -> np.ndarray:
    if exog_train is not None and np.allclose(exog_train, 0):
        exog_train = None
        exog_test = None
    result = fit_sarimax(train, source=source, exog=exog_train.reshape(-1, 1) if exog_train is not None else None)
    forecast = result.forecast(steps=steps, exog=exog_test.reshape(-1, 1) if exog_test is not None else None)
    return np.maximum(np.asarray(forecast, dtype=float), 0.0)


def _truncate(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event.get("event_id") or event.get("id"),
        "news_id": event.get("news_id"),
        "brand": event.get("brand"),
        "brand_name": event.get("brand_name"),
        "score": event.get("score"),
        "tag": event.get("tag"),
        "category": event.get("category"),
        "derivation": event.get("derivation"),
        "title": _truncate(event.get("title"), 240),
        "summary": _truncate(event.get("summary"), 500),
        "source": event.get("source"),
        "date": event.get("date"),
        "published_date": event.get("published_date"),
        "reason": _truncate(event.get("reason"), 500),
        "period_map": event.get("period_map"),
    }


def _compact_sentiment(sentiment: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": sentiment.get("event_id"),
        "brand": sentiment.get("brand"),
        "sentiment_score": sentiment.get("sentiment_score"),
        "duration_months": sentiment.get("duration_months"),
        "reasoning": _truncate(sentiment.get("reasoning"), 500),
        "method": sentiment.get("method"),
        "event": _compact_event(sentiment.get("event") or {}),
    }


def backtest_brand(brand: str, source: str, measure: str, *, use_llm: bool = False) -> dict[str, Any]:
    history = get_brand_history(brand, source, measure)
    cfg = model_config(source)
    holdout = min(cfg["holdout_steps"], max(1, len(history) // 4))
    if len(history) <= holdout + 8:
        raise ValueError(f"Insufficient history for {brand}/{source}: {len(history)}")
    train = history.iloc[:-holdout]
    test = history.iloc[-holdout:]

    conn = connect()
    try:
        ensure_events_raw_table(conn)
        all_cut_b_events = get_brand_events_cut_b(conn, brand, lookback_months=None)
    finally:
        conn.close()

    sentiments = score_events(brand, all_cut_b_events, use_llm=use_llm)
    full_periods = history.index.tolist()
    x_all = build_exogenous(sentiments, full_periods, source=source)
    x_train = x_all[: len(train)]
    x_test = x_all[len(train) :]

    baseline_pred = _forecast_with_optional_exog(train, source=source, steps=len(test), exog_train=None, exog_test=None)
    with_llm_pred = _forecast_with_optional_exog(train, source=source, steps=len(test), exog_train=x_train, exog_test=x_test)

    baseline_metrics = metrics(test, baseline_pred)
    with_llm_metrics = metrics(test, with_llm_pred)
    if with_llm_metrics["rmse"] < baseline_metrics["rmse"]:
        verdict = "with_llm_better"
    elif math.isclose(with_llm_metrics["rmse"], baseline_metrics["rmse"], rel_tol=0.02):
        verdict = "similar"
    else:
        verdict = "baseline_better"

    sentiment_methods = sorted({str(item.get("method")) for item in sentiments})
    compact_sentiments = [_compact_sentiment(item) for item in sentiments]
    return {
        "brand": brand,
        "source": source,
        "measure": measure,
        "combo": f"{source}.{measure}",
        "history_points": len(history),
        "holdout_points": len(test),
        "train_periods": train.index.tolist(),
        "test_periods": test.index.tolist(),
        "actual": [float(v) for v in test.tolist()],
        "baseline": {
            "forecast": [float(v) for v in baseline_pred.tolist()],
            "metrics": baseline_metrics,
        },
        "with_llm": {
            "forecast": [float(v) for v in with_llm_pred.tolist()],
            "metrics": with_llm_metrics,
            "exogenous_train_nonzero": int(np.count_nonzero(np.abs(x_train) > 1e-9)),
            "exogenous_test_nonzero": int(np.count_nonzero(np.abs(x_test) > 1e-9)),
        },
        "cut_b_event_count_all_history": len(all_cut_b_events),
        "sentiment_count": len(sentiments),
        "sentiment_methods": sentiment_methods,
        "sentiments": compact_sentiments,
        "verdict": verdict,
    }


def run_phase29_poc(*, use_llm: bool = False, persist: bool = True, output_path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    brands = {
        brand: backtest_brand(brand, spec["source"], spec["measure"], use_llm=use_llm)
        for brand, spec in POC_BRANDS.items()
    }
    report = {
        "phase": "29",
        "poc": True,
        "use_llm_requested": use_llm,
        "brands": brands,
    }
    if persist:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    report = run_phase29_poc(use_llm=args.use_llm, persist=True, output_path=Path(args.output))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
