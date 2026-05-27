from __future__ import annotations

import json
from datetime import date
from typing import Dict

from .config import MarketConfig


def _json_load(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {}


def _month_floor(snapshot_at) -> str:
    return snapshot_at.strftime("%Y-%m")


def _shift_month(yyyymm: str, delta: int) -> str:
    year, month = [int(x) for x in yyyymm.split("-")]
    month += delta
    while month <= 0:
        year -= 1
        month += 12
    while month > 12:
        year += 1
        month -= 12
    return f"{year:04d}-{month:02d}"


def _filter_history(history: dict, snapshot_at, months: int) -> dict:
    end = _month_floor(snapshot_at)
    start = _shift_month(end, -(months - 1))
    return {k: history[k] for k in sorted(history) if start <= k <= end}


def _sum_raw(raw_history: dict, months: list) -> float | None:
    vals = []
    for month in months:
        value = raw_history.get(month, {})
        if isinstance(value, dict):
            value = value.get("raw_value")
        if value is None:
            return None
        vals.append(float(value))
    return sum(vals)


def _mat_12m(metric_history: dict, raw_history: dict, snapshot_at) -> dict:
    end = _month_floor(snapshot_at)
    months = [_shift_month(end, -i) for i in range(11, -1, -1)]
    prev_months = [_shift_month(end, -i) for i in range(23, 11, -1)]
    latest = metric_history.get(end, {}) if isinstance(metric_history.get(end), dict) else {}
    current_sum = _sum_raw(raw_history, months)
    previous_sum = _sum_raw(raw_history, prev_months)
    growth_yoy = None
    if current_sum is not None and previous_sum not in (None, 0):
        growth_yoy = (current_sum - previous_sum) / previous_sum * 100
    return {
        "latest_month": end,
        "value": latest.get("mat") if latest else current_sum,
        "raw_value_12m": current_sum,
        "growth_yoy": growth_yoy,
    }


def _empty_metric():
    return {"unit_label": None, "history": {}, "mat_12m": {"latest_month": None, "value": None, "raw_value_12m": None, "growth_yoy": None}}


def build_market_context(
    brand: str,
    market_ids: Dict[str, list],
    db_conn,
    snapshot_at,
    config: MarketConfig,
) -> Dict:
    primary_market_id = market_ids.get("ml_ids", [None])[0] if market_ids.get("ml_ids") else None
    metric_keys = [f"{source}.{measure}" for source, measure in config.brand_metrics]
    brand_metrics = {key: _empty_metric() for key in metric_keys}

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT ml_id, brand_name, source, measure, unit_label, metric_history,
                   raw_value_history, extended_metric_history, overlay_data
            FROM mart_strategic_ml_brand_metric
            WHERE brand_name = %s AND computed_at <= %s
            ORDER BY ml_id ASC, source ASC, measure ASC
            """,
            (brand, snapshot_at.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")),
        )
        rows = cur.fetchall()

    for row in rows:
        key = f"{row['source']}.{row['measure']}"
        if key not in brand_metrics:
            continue
        metric_history = _json_load(row["metric_history"])
        raw_history = _json_load(row["raw_value_history"])
        brand_metrics[key] = {
            "unit_label": row["unit_label"],
            "history": _filter_history(metric_history, snapshot_at, config.lookback_months),
            "mat_12m": _mat_12m(metric_history, raw_history, snapshot_at) if config.include_mat_12m else {},
        }

    market_size = {key: {"history": {}, "hhi": {}} for key in metric_keys}
    market_label = None
    atc4_code = None
    if primary_market_id:
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT ml_id, ml_name, source, measure, market_size_series, hhi_series_5y
                FROM mart_strategic_ml_market_metric
                WHERE ml_id = %s AND computed_at <= %s
                ORDER BY source ASC, measure ASC
                """,
                (primary_market_id, snapshot_at.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")),
            )
            market_rows = cur.fetchall()
        for row in market_rows:
            market_label = market_label or row["ml_name"]
            key = f"{row['source']}.{row['measure']}"
            if key not in market_size:
                continue
            market_size[key] = {
                "history": _filter_history(_json_load(row["market_size_series"]), snapshot_at, config.lookback_months)
                if config.include_market_size
                else {},
                "hhi": _filter_history(_json_load(row["hhi_series_5y"]), snapshot_at, config.lookback_months)
                if config.include_hhi
                else {},
            }

    return {
        "primary_market_id": primary_market_id,
        "market_label": market_label,
        "atc4_code": atc4_code,
        "brand_metrics": brand_metrics,
        "market_size": market_size,
    }
