from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jw_chat_agent_poc.tools.query_layer.derived_models import (
    BrandKey,
    DerivedBrandInsight,
    DerivedBrandPoint,
    DerivedCompetitor,
    DerivedMarketPoint,
)
from jw_chat_agent_poc.tools.query_layer.derived_growth import (
    compound_growth,
    elapsed_months,
    growth,
    latest_brand_growth,
    latest_market_growth,
    terminal_streak,
    turning_point,
)

if TYPE_CHECKING:
    from jw_chat_agent_poc.tools.query_layer.store import MartRecord, MartSnapshot


@dataclass(frozen=True, slots=True)
class DerivedSnapshotIndex:
    market_points: dict[tuple[str, str, str, str], DerivedMarketPoint]
    brand_points: dict[BrandKey, DerivedBrandPoint]
    insights: dict[tuple[str, str, str, str], DerivedBrandInsight]

    @classmethod
    def build(cls, snapshot: MartSnapshot) -> DerivedSnapshotIndex:
        market_points: dict[tuple[str, str, str, str], DerivedMarketPoint] = {}
        brand_points: dict[BrandKey, DerivedBrandPoint] = {}
        insights: dict[tuple[str, str, str, str], DerivedBrandInsight] = {}
        keys = sorted({(row.ml_id, row.source, row.measure) for row in snapshot.records})
        for market, source, measure in keys:
            records = snapshot.market_records(market, source, measure)
            periods = snapshot.periods(market, source, measure)
            ranked_by_period: dict[str, tuple[tuple[str, float, float | None, int], ...]] = {}
            for period in periods:
                market_point, ranked = _period_points(snapshot, records, period)
                market_points[(market, source, measure, period)] = market_point
                ranked_by_period[period] = ranked
                rank_map = {row[0]: row for row in ranked}
                for record in records:
                    rank_row = rank_map.get(record.brand_name)
                    brand_points[(market, source, measure, record.brand_name, period)] = DerivedBrandPoint(
                        value_krw=snapshot.value_or_none(record, period),
                        share_pct=rank_row[2] if rank_row else None,
                        rank=rank_row[3] if rank_row else None,
                        source_status=snapshot.value_status(record, period),
                    )
            selected = periods[-10:]
            competitors = _competitor_map(
                brand_points,
                market,
                source,
                measure,
                selected[0] if selected else "",
                selected[-1] if selected else "",
                ranked_by_period.get(selected[-1], ()) if selected else (),
            )
            for record in records:
                insights[(market, source, measure, record.brand_name)] = _brand_insight(
                    market_points,
                    brand_points,
                    market,
                    source,
                    measure,
                    record.brand_name,
                    periods,
                    competitors.get(record.brand_name, ()),
                )
        return cls(market_points=market_points, brand_points=brand_points, insights=insights)

    def market_point(self, market: str, source: str, measure: str, period: str) -> DerivedMarketPoint:
        return self.market_points[(market, source, measure, period)]

    def brand_point(self, market: str, source: str, measure: str, brand: str, period: str) -> DerivedBrandPoint:
        return self.brand_points[(market, source, measure, brand, period)]

    def brand_insight(self, market: str, source: str, measure: str, brand: str) -> DerivedBrandInsight:
        return self.insights[(market, source, measure, brand)]


def _period_points(
    snapshot: MartSnapshot,
    records: tuple[MartRecord, ...],
    period: str,
) -> tuple[DerivedMarketPoint, tuple[tuple[str, float, float | None, int], ...]]:
    values = [(record, snapshot.value_or_none(record, period)) for record in records]
    complete = bool(values) and all(value is not None for _, value in values)
    total = sum(float(value) for _, value in values if value is not None) if complete else None
    ranked_values = sorted(((record, float(value)) for record, value in values if value is not None), key=lambda item: item[1], reverse=True)
    ranked: list[tuple[str, float, float | None, int]] = []
    for rank, (record, value) in enumerate(ranked_values, start=1):
        share = _stored_or_computed_share(record, period, value, total)
        ranked.append((record.brand_name, value, share, rank))
    shares = [row[2] for row in ranked if row[2] is not None]
    hhi = sum(float(share) ** 2 for share in shares) if shares else None
    top5 = [row[2] for row in ranked[:5]]
    cr5 = sum(float(share) for share in top5 if share is not None) if top5 and all(share is not None for share in top5) else None
    return DerivedMarketPoint(total, hhi, cr5, len(ranked)), tuple(ranked)


def _stored_or_computed_share(record: MartRecord, period: str, value: float, total: float | None) -> float | None:
    if total is None or total == 0:
        return None
    row = record.metric_history.get(period)
    stored = row.get("ms") if isinstance(row, dict) and not (len(period) == 4 and period.isdigit()) else None
    if isinstance(stored, int | float):
        numeric = float(stored)
        if math.isfinite(numeric):
            return numeric
    return value / total * 100


def _brand_insight(
    markets: dict[tuple[str, str, str, str], DerivedMarketPoint],
    brands: dict[BrandKey, DerivedBrandPoint],
    market: str,
    source: str,
    measure: str,
    brand: str,
    periods: tuple[str, ...],
    competitors: tuple[DerivedCompetitor, ...],
) -> DerivedBrandInsight:
    selected = periods[-10:]
    points = [brands[(market, source, measure, brand, period)] for period in selected]
    market_points = [markets[(market, source, measure, period)] for period in selected]
    missing = tuple(period for period, point in zip(selected, market_points, strict=True) if point.total_krw is None)
    brand_complete = bool(points) and all(point.value_krw is not None for point in points)
    market_complete = bool(market_points) and not missing
    first, last = (points[0], points[-1]) if points else (None, None)
    first_market, last_market = (market_points[0], market_points[-1]) if market_points else (None, None)
    brand_growth = growth(first.value_krw, last.value_krw) if brand_complete and first and last else None
    market_growth = growth(first_market.total_krw, last_market.total_krw) if market_complete and first_market and last_market else None
    elapsed = elapsed_months(selected[0], selected[-1]) if len(selected) > 1 else None
    brand_mom = latest_brand_growth(brands, market, source, measure, brand, periods, monthly=True)
    market_mom = latest_market_growth(markets, market, source, measure, periods, monthly=True)
    brand_yoy = latest_brand_growth(brands, market, source, measure, brand, periods, monthly=False)
    market_yoy = latest_market_growth(markets, market, source, measure, periods, monthly=False)
    shares = [(period, point.share_pct) for period, point in zip(selected, points, strict=True) if point.share_pct is not None]
    share_max = max(shares, key=lambda item: item[1]) if shares else (None, None)
    share_min = min(shares, key=lambda item: item[1]) if shares else (None, None)
    direction, streak = terminal_streak([float(value) for _, value in shares])
    turning, turning_kind = turning_point(shares)
    return DerivedBrandInsight(
        periods=selected,
        missing_periods=missing,
        share_start_pct=first.share_pct if first else None,
        share_end_pct=last.share_pct if last else None,
        share_delta_pctp=_delta(first.share_pct, last.share_pct) if first and last else None,
        sales_start_krw=first.value_krw if first else None,
        sales_end_krw=last.value_krw if last else None,
        sales_delta_krw=_delta(first.value_krw, last.value_krw) if first and last else None,
        market_start_krw=first_market.total_krw if first_market else None,
        market_end_krw=last_market.total_krw if last_market else None,
        brand_growth_pct=brand_growth,
        market_growth_pct=market_growth,
        excess_growth_pctp=_delta(market_growth, brand_growth),
        brand_mom_pct=brand_mom,
        market_mom_pct=market_mom,
        brand_yoy_pct=brand_yoy,
        market_yoy_pct=market_yoy,
        brand_cmgr_pct=compound_growth(first.value_krw, last.value_krw, elapsed, 1)
        if brand_complete and first and last
        else None,
        market_cmgr_pct=compound_growth(first_market.total_krw, last_market.total_krw, elapsed, 1)
        if market_complete and first_market and last_market
        else None,
        brand_cqgr_pct=compound_growth(first.value_krw, last.value_krw, elapsed, 3)
        if brand_complete and first and last
        else None,
        market_cqgr_pct=compound_growth(first_market.total_krw, last_market.total_krw, elapsed, 3)
        if market_complete and first_market and last_market
        else None,
        rank_start=first.rank if first else None,
        rank_end=last.rank if last else None,
        share_max_pct=share_max[1],
        share_max_period=share_max[0],
        share_min_pct=share_min[1],
        share_min_period=share_min[0],
        turning_point=turning,
        turning_kind=turning_kind,
        trend_direction=direction,
        trend_months=streak,
        hhi_end=last_market.hhi if last_market else None,
        cr5_end_pct=last_market.cr5_pct if last_market else None,
        denominator_end=last_market.denominator if last_market else 0,
        competitors=competitors,
    )


def _competitor_map(
    brands: dict[BrandKey, DerivedBrandPoint],
    market: str,
    source: str,
    measure: str,
    start_period: str,
    end_period: str,
    ranked: tuple[tuple[str, float, float | None, int], ...],
) -> dict[str, tuple[DerivedCompetitor, ...]]:
    candidates = ranked[:4]
    result: dict[str, tuple[DerivedCompetitor, ...]] = {}
    for anchor, *_ in ranked:
        peers: list[DerivedCompetitor] = []
        for brand, value, share, rank in candidates:
            if brand == anchor:
                continue
            start = brands.get((market, source, measure, brand, start_period))
            peers.append(
                DerivedCompetitor(
                    brand=brand,
                    rank_start=start.rank if start else None,
                    rank_end=rank,
                    sales_start_krw=start.value_krw if start else None,
                    sales_end_krw=value,
                    share_start_pct=start.share_pct if start else None,
                    share_end_pct=share,
                )
            )
            if len(peers) == 3:
                break
        result[anchor] = tuple(peers)
    return result


def _delta(start: float | None, end: float | None) -> float | None:
    return float(end) - float(start) if start is not None and end is not None else None
