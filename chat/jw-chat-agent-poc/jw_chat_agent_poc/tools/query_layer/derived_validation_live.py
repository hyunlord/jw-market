from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from jw_chat_agent_poc.tools.query_layer.derived_validation_math import (
    compound,
    delta,
    elapsed_months,
    ends,
    growth,
    shift_month,
    shift_year,
    terminal_streak,
    turning_point,
)
from jw_chat_agent_poc.tools.query_layer.store import FAILED_VALUE_STATUSES, MartRecord, MartSnapshot


GroupKey = tuple[str, str, str]
MarketKey = tuple[str, str, str, str]
BrandKey = tuple[str, str, str, str, str]
InsightKey = tuple[str, str, str, str]
RankedRow = tuple[str, float, float | None, int]


@dataclass(frozen=True, slots=True)
class LiveDerivedCensus:
    periods: dict[GroupKey, tuple[str, ...]]
    market_points: dict[MarketKey, tuple[float | None, float | None, float | None, int]]
    brand_points: dict[BrandKey, tuple[float | None, float | None, int | None, str]]
    rankings: dict[MarketKey, tuple[RankedRow, ...]]

    @classmethod
    def build(cls, snapshot: MartSnapshot) -> LiveDerivedCensus:
        grouped: dict[GroupKey, list[MartRecord]] = {}
        for record in snapshot.records:
            grouped.setdefault((record.ml_id, record.source, record.measure), []).append(record)
        periods_by_group: dict[GroupKey, tuple[str, ...]] = {}
        market_points: dict[MarketKey, tuple[float | None, float | None, float | None, int]] = {}
        brand_points: dict[BrandKey, tuple[float | None, float | None, int | None, str]] = {}
        rankings: dict[MarketKey, tuple[RankedRow, ...]] = {}
        for group, records in sorted(grouped.items()):
            periods = tuple(sorted({period for record in records for period in record.metric_history}))
            periods_by_group[group] = periods
            for period in periods:
                market_point, ranked = _period_census(records, period)
                market_key = (*group, period)
                market_points[market_key] = market_point
                rankings[market_key] = ranked
                ranks = {row[0]: row for row in ranked}
                for record in records:
                    rank_row = ranks.get(record.brand_name)
                    brand_points[(*group, record.brand_name, period)] = (
                        _value(record, period),
                        rank_row[2] if rank_row else None,
                        rank_row[3] if rank_row else None,
                        _status(record, period),
                    )
        return cls(periods_by_group, market_points, brand_points, rankings)

    def insight(self, key: InsightKey) -> dict[str, Any]:
        market, source, measure, brand = key
        group = (market, source, measure)
        periods = self.periods[group]
        selected = periods[-10:]
        points = tuple(self.brand_points[(*group, brand, period)] for period in selected)
        markets = tuple(self.market_points[(*group, period)] for period in selected)
        values = tuple(point[0] for point in points)
        shares = tuple(point[1] for point in points)
        ranks = tuple(point[2] for point in points)
        totals = tuple(point[0] for point in markets)
        missing = tuple(period for period, total in zip(selected, totals, strict=True) if total is None)
        brand_complete = bool(values) and all(value is not None for value in values)
        market_complete = bool(totals) and not missing
        first_value, last_value = ends(values)
        first_total, last_total = ends(totals)
        first_share, last_share = ends(shares)
        first_rank, last_rank = ends(ranks)
        brand_growth = growth(first_value, last_value) if brand_complete else None
        market_growth = growth(first_total, last_total) if market_complete else None
        extrema = tuple((period, share) for period, share in zip(selected, shares, strict=True) if share is not None)
        maximum = max(extrema, key=lambda item: item[1]) if extrema else (None, None)
        minimum = min(extrema, key=lambda item: item[1]) if extrema else (None, None)
        direction, streak = terminal_streak(tuple(float(value) for _, value in extrema))
        turning, turning_kind = turning_point(extrema)
        elapsed = elapsed_months(selected[0], selected[-1]) if len(selected) > 1 else None
        latest = periods[-1] if periods else ""
        latest_point = self.brand_points.get((*group, brand, latest))
        latest_market = self.market_points.get((*group, latest))
        ranked = self.rankings.get((*group, latest), ())
        top5 = tuple(row[2] for row in ranked[:5])
        return {
            "periods": selected,
            "missing_periods": missing,
            "share_start_pct": first_share,
            "share_end_pct": last_share,
            "share_delta_pctp": delta(first_share, last_share),
            "sales_start_krw": first_value,
            "sales_end_krw": last_value,
            "sales_delta_krw": delta(first_value, last_value),
            "market_start_krw": first_total,
            "market_end_krw": last_total,
            "brand_growth_pct": brand_growth,
            "market_growth_pct": market_growth,
            "excess_growth_pctp": delta(market_growth, brand_growth),
            "brand_mom_pct": self._brand_growth(group, brand, latest, shift_month(latest)),
            "market_mom_pct": self._market_growth(group, latest, shift_month(latest)),
            "brand_yoy_pct": self._brand_growth(group, brand, latest, shift_year(latest)),
            "market_yoy_pct": self._market_growth(group, latest, shift_year(latest)),
            "brand_cmgr_pct": compound(first_value, last_value, elapsed, 1) if brand_complete else None,
            "market_cmgr_pct": compound(first_total, last_total, elapsed, 1) if market_complete else None,
            "brand_cqgr_pct": compound(first_value, last_value, elapsed, 3) if brand_complete else None,
            "market_cqgr_pct": compound(first_total, last_total, elapsed, 3) if market_complete else None,
            "rank_start": first_rank,
            "rank_end": last_rank,
            "share_max_pct": maximum[1],
            "share_max_period": maximum[0],
            "share_min_pct": minimum[1],
            "share_min_period": minimum[0],
            "turning_point": turning,
            "turning_kind": turning_kind,
            "trend_direction": direction,
            "trend_months": streak,
            "hhi_end": latest_market[1] if latest_market else None,
            "cr5_end_pct": sum(float(value) for value in top5) if top5 and all(value is not None for value in top5) else None,
            "denominator_end": latest_market[3] if latest_market else 0,
            "competitors": self._competitors(group, brand, selected, ranked),
        }

    def _brand_growth(self, group: GroupKey, brand: str, latest: str, previous: str) -> float | None:
        start = self.brand_points.get((*group, brand, previous))
        end = self.brand_points.get((*group, brand, latest))
        return growth(start[0] if start else None, end[0] if end else None)

    def _market_growth(self, group: GroupKey, latest: str, previous: str) -> float | None:
        start = self.market_points.get((*group, previous))
        end = self.market_points.get((*group, latest))
        return growth(start[0] if start else None, end[0] if end else None)

    def _competitors(
        self,
        group: GroupKey,
        anchor: str,
        selected: tuple[str, ...],
        ranked: tuple[RankedRow, ...],
    ) -> tuple[dict[str, Any], ...]:
        if not selected:
            return ()
        peers: list[dict[str, Any]] = []
        for brand, value, share, rank in ranked[:4]:
            if brand == anchor:
                continue
            start = self.brand_points.get((*group, brand, selected[0]))
            peers.append(
                {
                    "brand": brand,
                    "rank_start": start[2] if start else None,
                    "rank_end": rank,
                    "sales_start_krw": start[0] if start else None,
                    "sales_end_krw": value,
                    "share_start_pct": start[1] if start else None,
                    "share_end_pct": share,
                }
            )
            if len(peers) == 3:
                break
        return tuple(peers)


def _period_census(
    records: list[MartRecord],
    period: str,
) -> tuple[tuple[float | None, float | None, float | None, int], tuple[RankedRow, ...]]:
    values = tuple((record, _value(record, period)) for record in records)
    complete = bool(values) and all(value is not None for _, value in values)
    total = sum(float(value) for _, value in values if value is not None) if complete else None
    ordered = sorted(((record, float(value)) for record, value in values if value is not None), key=lambda item: item[1], reverse=True)
    ranked = tuple(
        (record.brand_name, value, _share(record, period, value, total), rank)
        for rank, (record, value) in enumerate(ordered, start=1)
    )
    shares = tuple(row[2] for row in ranked if row[2] is not None)
    top5 = tuple(row[2] for row in ranked[:5])
    hhi = sum(float(value) ** 2 for value in shares) if shares else None
    cr5 = sum(float(value) for value in top5) if top5 and all(value is not None for value in top5) else None
    return (total, hhi, cr5, len(ranked)), ranked


def _value(record: MartRecord, period: str) -> float | None:
    row = record.metric_history.get(period)
    if not isinstance(row, dict) or _status(record, period) in FAILED_VALUE_STATUSES:
        return None
    value = row.get("raw_value")
    if not isinstance(value, int | float):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _status(record: MartRecord, period: str) -> str:
    row = record.metric_history.get(period)
    if not isinstance(row, dict):
        return "missing"
    raw_value = row.get("raw_value")
    if isinstance(raw_value, int | float) and not math.isfinite(float(raw_value)):
        return "missing"
    status = str(row.get("source_status", row.get("status")) or "OK")
    return status if status in FAILED_VALUE_STATUSES else "OK"


def _share(record: MartRecord, period: str, value: float, total: float | None) -> float | None:
    if total in {None, 0}:
        return None
    row = record.metric_history.get(period)
    stored = row.get("ms") if isinstance(row, dict) else None
    if isinstance(stored, int | float):
        numeric = float(stored)
        if math.isfinite(numeric):
            return numeric
    return value / float(total) * 100
