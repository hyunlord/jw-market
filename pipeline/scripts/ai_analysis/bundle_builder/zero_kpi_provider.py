from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Protocol

from bundle_builder.agent2_zero_template import KpiSnapshot


class ZeroKpiSnapshotProvider(Protocol):
    def get_snapshot(self, brand_key: str, brand_name: str) -> KpiSnapshot:
        ...


@dataclass(frozen=True, slots=True)
class EmptyZeroKpiSnapshotProvider:
    def get_snapshot(self, brand_key: str, brand_name: str) -> KpiSnapshot:
        return KpiSnapshot(brand=brand_name)


@dataclass(frozen=True, slots=True)
class _MetricPoint:
    period: str
    value: float


def json_load(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if value is not None else {}


def metric_points(metric_history: Any) -> list[_MetricPoint]:
    history = json_load(metric_history)
    if not isinstance(history, dict):
        return []
    points: list[_MetricPoint] = []
    for period, item in history.items():
        if not isinstance(item, dict):
            continue
        value = _to_float(item.get("raw_value"))
        if value is None:
            continue
        points.append(_MetricPoint(str(period), value))
    return sorted(points, key=lambda point: _period_sort_key(point.period))


def brand_cagr_pct(metric_history: Any) -> float | None:
    positives = [point for point in metric_points(metric_history) if point.value > 0]
    if len(positives) < 2:
        return None
    first = positives[0]
    latest = positives[-1]
    elapsed_years = _elapsed_years(first.period, latest.period, len(positives))
    if elapsed_years <= 0:
        return None
    return (math.pow(latest.value / first.value, 1 / elapsed_years) - 1) * 100


def snapshot_from_metric_rows(rows: list[dict[str, Any]], brand_key: str, brand_name: str) -> KpiSnapshot:
    snapshot = snapshots_from_metric_rows(rows).get(brand_key)
    if snapshot is None:
        return KpiSnapshot(brand=brand_name)
    return snapshot_with_brand(snapshot, brand_name)


def snapshots_from_metric_rows(rows: list[dict[str, Any]]) -> dict[str, KpiSnapshot]:
    enriched_rows: list[dict[str, Any]] = []
    for row in rows:
        points = metric_points(row.get("metric_history"))
        positive_latest = next((point for point in reversed(points) if point.value > 0), None)
        if positive_latest is None:
            continue
        enriched = dict(row)
        enriched["latest_period"] = positive_latest.period
        enriched["latest_value"] = positive_latest.value
        enriched_rows.append(enriched)

    market_rows_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in enriched_rows:
        market_key = (str(row.get("source") or ""), str(row.get("atc4_code") or ""))
        market_rows_by_key.setdefault(market_key, []).append(row)

    best_by_brand: dict[str, tuple[float, KpiSnapshot]] = {}
    for market_rows in market_rows_by_key.values():
        market_rows.sort(key=lambda row: (-float(row["latest_value"]), str(row.get("brand_name") or ""), str(row.get("brand_key") or "")))
        total = sum(float(row["latest_value"]) for row in market_rows)
        if total <= 0:
            continue
        hhi = sum(math.pow(float(row["latest_value"]) / total * 100, 2) for row in market_rows)
        for idx, row in enumerate(market_rows, start=1):
            key = str(row.get("brand_key") or "")
            if not key:
                continue
            latest_value = float(row["latest_value"])
            previous = best_by_brand.get(key)
            if previous is not None and latest_value <= previous[0]:
                continue
            market_name = str(row.get("atc4_desc") or row.get("atc4_code") or "").strip() or None
            best_by_brand[key] = (
                latest_value,
                KpiSnapshot(
                    brand=str(row.get("brand_name") or key),
                    market_name=market_name,
                    rank=idx,
                    share_pct=latest_value / total * 100,
                    cagr_pct=brand_cagr_pct(row.get("metric_history")),
                    hhi=hhi,
                    market_size_recent=total,
                ),
            )

    return {brand_key: snapshot for brand_key, (_value, snapshot) in best_by_brand.items()}


class BatchGeneralZeroKpiSnapshotProvider:
    """Read general mart KPI rows once and serve zero-template snapshots by brand_key."""

    def __init__(self, db_conn: Any):
        self.db_conn = db_conn
        self._snapshots: dict[str, KpiSnapshot] | None = None

    def get_snapshot(self, brand_key: str, brand_name: str) -> KpiSnapshot:
        snapshots = self._load_snapshots()
        snapshot = snapshots.get(brand_key)
        if snapshot is None:
            return KpiSnapshot(brand=brand_name)
        return snapshot_with_brand(snapshot, brand_name)

    def _load_snapshots(self) -> dict[str, KpiSnapshot]:
        if self._snapshots is not None:
            return self._snapshots
        rows = self._fetch_sales_rows()
        self._snapshots = snapshots_from_metric_rows(rows)
        return self._snapshots

    def _fetch_sales_rows(self) -> list[dict[str, Any]]:
        cursor = self.db_conn.cursor()
        cursor.execute(
            """
            SELECT brand_key, brand_name, atc4_code, atc4_desc, source, measure, metric_history
            FROM mart_general_brand_metric
            WHERE measure = 'sales'
              AND brand_key IS NOT NULL
              AND brand_key <> ''
              AND atc4_code IS NOT NULL
              AND atc4_code <> ''
            """
        )
        return list(cursor.fetchall())


def snapshot_with_brand(snapshot: KpiSnapshot, brand_name: str) -> KpiSnapshot:
    return KpiSnapshot(
        brand=brand_name,
        market_name=snapshot.market_name,
        rank=snapshot.rank,
        share_pct=snapshot.share_pct,
        cagr_pct=snapshot.cagr_pct,
        ei=snapshot.ei,
        momentum=snapshot.momentum,
        hhi=snapshot.hhi,
        market_size_recent=snapshot.market_size_recent,
        first_positive_period=snapshot.first_positive_period,
        is_new=snapshot.is_new,
    )


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _period_sort_key(period: str) -> tuple[int, float, str]:
    match = re.match(r"^(20\d{2})-Q([1-4])$", period)
    if match:
        return int(match.group(1)), (int(match.group(2)) - 1) / 4, period
    match = re.match(r"^(20\d{2})-(\d{2})$", period)
    if match:
        return int(match.group(1)), (int(match.group(2)) - 1) / 12, period
    match = re.match(r"^(20\d{2})$", period)
    if match:
        return int(match.group(1)), 0.0, period
    return 0, 0.0, period


def _elapsed_years(first_period: str, latest_period: str, point_count: int) -> float:
    first_year, first_offset, _ = _period_sort_key(first_period)
    latest_year, latest_offset, _ = _period_sort_key(latest_period)
    if first_year and latest_year:
        return (latest_year + latest_offset) - (first_year + first_offset)
    return max(point_count - 1, 1)
