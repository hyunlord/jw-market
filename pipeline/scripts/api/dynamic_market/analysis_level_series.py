"""Shared series helpers for dynamic analysis-level adapters."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


ETL_DIR = Path(__file__).resolve().parents[2] / "etl"
if str(ETL_DIR) not in sys.path:
    sys.path.insert(0, str(ETL_DIR))

from pipeline.scripts.etl import build_cache_cause as cause_builder


def history_by_period(history: Any) -> dict[str, float]:
    if not isinstance(history, dict):
        return {}
    return {str(period): float(value or 0.0) for period, value in history.items()}


def metric_history_from_periods(
    *,
    history_by_period: dict[str, float],
    totals_by_period: dict[str, float],
    rank: int = 0,
) -> dict[str, dict[str, float | int]]:
    history: dict[str, dict[str, float | int]] = {}
    for period, value in history_by_period.items():
        total = totals_by_period.get(period, 0.0)
        history[period] = {
            "raw_value": value,
            "value": value,
            "ms": round(value / total * 100, 4) if total else 0.0,
            "rank": rank,
        }
    return history


def with_dimension_series_from_labels(
    dimension_data_raw: Any,
    by_dimension_raw: Any,
    history_by_period: dict[str, float],
) -> str:
    encoded, _, _ = with_dimension_series_from_labels_decoded(
        dimension_data_raw,
        by_dimension_raw,
        history_by_period,
    )
    return encoded


def with_dimension_series_from_labels_decoded(
    dimension_data_raw: Any,
    by_dimension_raw: Any,
    history_by_period: dict[str, float],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    dimension_data = cause_builder.decode_json(dimension_data_raw)
    if not isinstance(dimension_data, dict):
        dimension_data = {}
    by_dimension = cause_builder.decode_json(by_dimension_raw)
    if not isinstance(by_dimension, dict):
        by_dimension = {}
    metric_series = {
        str(period): {"raw_value": float(value or 0.0)}
        for period, value in history_by_period.items()
    }
    if not metric_series:
        return (
            json.dumps(dimension_data, ensure_ascii=False, sort_keys=True),
            dimension_data,
            by_dimension,
        )
    for field, label in by_dimension.items():
        field_data = dimension_data.setdefault(str(field), {})
        if not isinstance(field_data, dict):
            continue
        for item in cause_builder._split_atomic_dimension("", label):
            field_data.setdefault(str(item), metric_series)
    return (
        json.dumps(dimension_data, ensure_ascii=False, sort_keys=True),
        dimension_data,
        by_dimension,
    )
