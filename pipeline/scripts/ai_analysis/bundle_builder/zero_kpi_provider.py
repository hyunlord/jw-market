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


@dataclass(frozen=True, slots=True)
class SourcedKpiSnapshot(KpiSnapshot):
    """KPI snapshot plus the mart source selected for deterministic rendering."""

    source: str | None = None


@dataclass(frozen=True, slots=True)
class _MetricRow:
    brand_key: str
    brand_name: str
    atc4_code: str
    atc4_desc: str
    source: str
    history: dict[str, float]


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
        value = _to_float(item.get("raw_value")) if isinstance(item, dict) else _to_float(item)
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
    parsed_rows = [parsed for row in rows if (parsed := _parse_metric_row(row)) is not None]
    snapshots_by_source = {
        source: _snapshots_for_source([row for row in parsed_rows if row.source == source], source)
        for source in ("ubist", "iqvia_nsa")
    }
    selected: dict[str, KpiSnapshot] = {}
    for source in ("ubist", "iqvia_nsa"):
        for brand_key, snapshot in snapshots_by_source[source].items():
            selected.setdefault(brand_key, snapshot)
    return selected


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
            SELECT brand_key, brand_name, atc4_code, atc4_desc, source, measure,
                   raw_value_history AS metric_history
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
    return SourcedKpiSnapshot(
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
        source=getattr(snapshot, "source", None),
    )


def _parse_metric_row(row: dict[str, Any]) -> _MetricRow | None:
    brand_key = str(row.get("brand_key") or "").strip()
    source = str(row.get("source") or "").strip().lower()
    atc4_code = str(row.get("atc4_code") or "").strip().upper()
    if not brand_key or source not in {"ubist", "iqvia_nsa"} or not atc4_code:
        return None
    history = {point.period: point.value for point in metric_points(row.get("metric_history"))}
    if not history:
        return None
    return _MetricRow(
        brand_key=brand_key,
        brand_name=str(row.get("brand_name") or brand_key),
        atc4_code=_canonical_atc4_code(atc4_code, source),
        atc4_desc=str(row.get("atc4_desc") or atc4_code).strip(),
        source=source,
        history=history,
    )


def _snapshots_for_source(rows: list[_MetricRow], source: str) -> dict[str, KpiSnapshot]:
    scopes_by_brand: dict[str, set[str]] = {}
    for row in rows:
        scopes_by_brand.setdefault(row.brand_key, set()).add(row.atc4_code)
    brands_by_scope: dict[tuple[str, ...], list[str]] = {}
    for brand_key, codes in scopes_by_brand.items():
        brands_by_scope.setdefault(tuple(sorted(codes)), []).append(brand_key)

    snapshots: dict[str, KpiSnapshot] = {}
    for scope, target_brands in brands_by_scope.items():
        scope_rows = [row for row in rows if row.atc4_code in scope]
        histories = _aggregate_brand_histories(scope_rows)
        if not histories:
            continue
        latest_period = max(
            (period for history in histories.values() for period in history),
            key=_period_sort_key,
        )
        latest_values = {brand_key: history.get(latest_period, 0.0) for brand_key, history in histories.items()}
        market_size_recent = sum(latest_values.values())
        rank_by_brand = _rank_by_latest_value(histories, latest_values)
        hhi = (
            sum(math.pow(value / market_size_recent * 100, 2) for value in latest_values.values())
            if market_size_recent > 0
            else None
        )
        rows_by_brand = {row.brand_key: row for row in scope_rows}
        for brand_key in target_brands:
            row = rows_by_brand.get(brand_key)
            if row is None:
                continue
            snapshots[brand_key] = SourcedKpiSnapshot(
                brand=row.brand_name,
                market_name=row.atc4_desc or None,
                rank=rank_by_brand.get(brand_key),
                share_pct=(latest_values[brand_key] / market_size_recent * 100) if market_size_recent > 0 else None,
                # Template growth/decline language is intentionally brand-level, not market CAGR.
                cagr_pct=brand_cagr_pct(histories[brand_key]),
                hhi=hhi,
                market_size_recent=market_size_recent,
                source=source,
            )
    return snapshots


def _aggregate_brand_histories(rows: list[_MetricRow]) -> dict[str, dict[str, float]]:
    histories: dict[str, dict[str, float]] = {}
    for row in rows:
        aggregate = histories.setdefault(row.brand_key, {})
        for period, value in row.history.items():
            aggregate[period] = aggregate.get(period, 0.0) + value
    return histories


def _rank_by_latest_value(
    histories: dict[str, dict[str, float]],
    latest_values: dict[str, float],
) -> dict[str, int]:
    cumulative_by_brand = {brand_key: sum(history.values()) for brand_key, history in histories.items()}
    ranked = sorted(
        histories,
        key=lambda brand_key: (
            0 if latest_values[brand_key] > 0 else 1,
            -latest_values[brand_key] if latest_values[brand_key] > 0 else 0.0,
            -cumulative_by_brand[brand_key],
            brand_key,
        ),
    )
    return {brand_key: rank for rank, brand_key in enumerate(ranked, start=1)}


def _canonical_atc4_code(value: str, source: str) -> str:
    if source != "ubist":
        return value
    native_to_canonical = {"A2B2": "A02B2", "A6B2": "A06B2", "C1D": "C01D0", "G4C0": "G04C0"}
    if value in native_to_canonical:
        return native_to_canonical[value]
    if re.fullmatch(r"[A-Z]\d[A-Z]\d", value):
        return f"{value[0]}0{value[1:]}"
    return value


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
