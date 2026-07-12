"""View-agnostic metric aggregation for dynamic markets."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math

from pipeline.scripts.api import db
from pipeline.scripts.api.dynamic_market.types import (
    AggregatedMetrics,
    BrandMetric,
    BrandRef,
    PeriodRange,
    quote_identifier,
)


@dataclass(frozen=True, slots=True)
class MetricAggregator:
    """Aggregate mart metric histories for a resolved brand set.

    The aggregator intentionally receives only brand keys plus source/measure.
    It does not know whether those keys came from the general resolver, a future
    strategic overlay resolver, or a predefined market.  This keeps ranking,
    HHI, CAGR, and monthly-series math reusable across views.
    """

    mart_db: str

    def aggregate(
        self,
        *,
        brands: tuple[BrandRef, ...],
        source: str,
        measure: str,
        period_range: PeriodRange,
        top_n: int,
    ) -> AggregatedMetrics:
        if not brands:
            return AggregatedMetrics(source, measure, "", 0.0, None, None, (), (), ())
        rows = self._load_metric_rows(brands=brands, source=source, measure=measure)
        brand_metrics, monthly_totals = self._aggregate_rows(rows, period_range=period_range)
        market_size = float(sum(monthly_totals.values()))
        ranked = tuple(
            BrandMetric(
                brand_key=item.brand_key,
                brand_name=item.brand_name,
                atc4_code=item.atc4_code,
                atc4_desc=item.atc4_desc,
                total_value=item.total_value,
                market_share_pct=round((item.total_value / market_size) * 100, 6) if market_size > 0 else 0.0,
                rank=index,
                latest_period=item.latest_period,
                latest_value=item.latest_value,
                monthly_series=item.monthly_series,
            )
            for index, item in enumerate(sorted(brand_metrics, key=lambda row: (-row.total_value, row.brand_key)), start=1)
        )
        monthly_series = tuple({"period": period, "market_size": value} for period, value in sorted(monthly_totals.items()))
        unit_label = str(rows[0].get("unit_label", "")) if rows else ""
        return AggregatedMetrics(
            source=source,
            measure=measure,
            unit_label=unit_label,
            market_size=market_size,
            hhi=compute_hhi(ranked),
            cagr=compute_cagr(monthly_series),
            monthly_series=monthly_series,
            brands=ranked[:top_n],
            all_brands=ranked,
        )

    def _load_metric_rows(
        self,
        *,
        brands: tuple[BrandRef, ...],
        source: str,
        measure: str,
    ) -> list[dict]:
        mart_db = quote_identifier(self.mart_db)
        scope_sql, scope_params = brand_scope_predicate(brands)
        return db.fetch_all(
            f"""
            SELECT brand_key, brand_name, atc4_code, atc4_desc, source, measure, unit_label, raw_value_history
            FROM {mart_db}.mart_general_brand_metric
            WHERE source = %s
              AND measure = %s
              AND {scope_sql}
            ORDER BY brand_name, brand_key
            """,
            (source, measure, *scope_params),
        )

    def _aggregate_rows(
        self,
        rows: list[dict],
        *,
        period_range: PeriodRange,
    ) -> tuple[list[BrandMetric], dict[str, float]]:
        brand_metrics: list[BrandMetric] = []
        monthly_totals: dict[str, float] = {}
        for row in rows:
            history = parse_history(str(row["raw_value_history"]))
            filtered = filter_periods(history, period_range)
            for period, value in filtered.items():
                monthly_totals[period] = monthly_totals.get(period, 0.0) + value
            latest_period = max(filtered) if filtered else None
            brand_metrics.append(
                BrandMetric(
                    brand_key=str(row["brand_key"]),
                    brand_name=str(row["brand_name"]),
                    atc4_code=str(row["atc4_code"]),
                    atc4_desc=str(row.get("atc4_desc") or ""),
                    total_value=float(sum(filtered.values())),
                    market_share_pct=0.0,
                    rank=0,
                    latest_period=latest_period,
                    latest_value=filtered.get(latest_period) if latest_period else None,
                    monthly_series=tuple({"period": period, "value": value} for period, value in sorted(filtered.items())),
                )
            )
        return brand_metrics, monthly_totals


def parse_history(raw: str) -> dict[str, float]:
    """Parse mart JSON history into month -> numeric value."""

    payload = json.loads(raw)
    return {str(period): float(value or 0.0) for period, value in payload.items()}


def filter_periods(history: dict[str, float], period_range: PeriodRange) -> dict[str, float]:
    """Apply an inclusive ``YYYY-MM`` range to a metric history."""

    return {
        period: value
        for period, value in history.items()
        if (period_range.start is None or period >= period_range.start)
        and (period_range.end is None or period <= period_range.end)
    }


def brand_scope_predicate(brands: tuple[BrandRef, ...]) -> tuple[str, tuple[str, ...]]:
    """Return SQL that preserves the resolver's market grain.

    General-view filters resolve brand rows at ``brand_key + atc4_code`` grain.
    A few brands appear in more than one ATC4 bucket, so reloading by brand key
    alone would leak rows outside the caller-defined market.  Strategic-view
    stubs do not yet carry ATC4 overlay keys; those intentionally fall back to
    brand-key scope until the real strategic resolver is implemented.
    """

    if all(brand.atc4_code for brand in brands):
        pairs = tuple((brand.brand_key, brand.atc4_code) for brand in brands)
        predicates = " OR ".join(["(brand_key = %s AND atc4_code = %s)"] * len(pairs))
        params = tuple(value for pair in pairs for value in pair)
        return f"({predicates})", params

    brand_keys = tuple(brand.brand_key for brand in brands)
    placeholders = ", ".join(["%s"] * len(brand_keys))
    return f"brand_key IN ({placeholders})", brand_keys


def compute_hhi(brands: tuple[BrandMetric, ...]) -> float | None:
    """Compute HHI from period-window market shares.

    HHI is defined as the sum of squared percentage shares.  A monopoly is
    therefore 10,000; an empty/zero market has no meaningful HHI.
    """

    if not brands:
        return None
    return round(sum(item.market_share_pct * item.market_share_pct for item in brands), 6)


def compute_cagr(monthly_series: tuple[dict[str, float | str], ...]) -> float | None:
    """Compute annualized growth from first to last positive monthly value."""

    positive = [(str(item["period"]), float(item["market_size"])) for item in monthly_series if float(item["market_size"]) > 0]
    if len(positive) < 2:
        return None
    first_period, first_value = positive[0]
    last_period, last_value = positive[-1]
    months = month_distance(first_period, last_period)
    if months <= 0:
        return None
    return round((math.pow(last_value / first_value, 12 / months) - 1) * 100, 6)


def month_distance(start: str, end: str) -> int:
    """Return elapsed months between ``YYYY-MM`` or ``YYYY-Qn`` labels."""

    def month_index(period: str) -> int:
        year_text, part = period.split("-", 1)
        year = int(year_text)
        if part.startswith("Q"):
            quarter = int(part[1:])
            if quarter not in {1, 2, 3, 4}:
                raise ValueError(f"invalid quarter period: {period}")
            month = (quarter - 1) * 3 + 1
        else:
            month = int(part)
            if month not in range(1, 13):
                raise ValueError(f"invalid month period: {period}")
        return year * 12 + month

    return month_index(end) - month_index(start)
