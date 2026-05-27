from __future__ import annotations

from typing import Optional


def _shift_month(yyyymm: str, delta: int) -> str:
    year, month = [int(part) for part in yyyymm.split("-")]
    month += delta
    while month <= 0:
        year -= 1
        month += 12
    while month > 12:
        year += 1
        month -= 12
    return f"{year:04d}-{month:02d}"


def find_latest_actual_period(metric_history: dict) -> Optional[str]:
    if not metric_history:
        return None
    return max(str(key) for key in metric_history.keys())


def compute_mat_12m_absolute(
    metric_history: dict,
    target_period: str,
) -> dict:
    months = [_shift_month(target_period, -offset) for offset in range(11, -1, -1)]
    total = 0.0
    missing = []
    for month in months:
        point = metric_history.get(month)
        raw_value = point.get("raw_value") if isinstance(point, dict) else None
        if raw_value is None:
            missing.append(month)
            continue
        total += float(raw_value)

    target = metric_history.get(target_period) or {}
    return {
        "latest_period": target_period,
        "value": total,
        "raw_value_12m": total,
        "growth_yoy_pct": target.get("mat") if isinstance(target, dict) else None,
        "missing_months": missing,
    }
