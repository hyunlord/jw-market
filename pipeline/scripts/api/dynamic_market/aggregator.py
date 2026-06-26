"""View-agnostic metric aggregation for dynamic markets."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math

from pipeline.etl.io.mart.filter_dimension_metric import FILTER_DIMENSION_TABLE
from pipeline.scripts.api import db
from pipeline.scripts.api.dynamic_market.types import (
    AggregatedMetrics,
    BrandMetric,
    BrandRef,
    DimensionFilter,
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
        dimension_filters: tuple[DimensionFilter, ...] = (),
    ) -> AggregatedMetrics:
        if not brands:
            return AggregatedMetrics(source, measure, "", 0.0, None, None, (), (), ())
        if dimension_filters:
            rows = self._load_sidecar_metric_rows(
                brands=brands,
                source=source,
                measure=measure,
                dimension_filters=dimension_filters,
            )
        else:
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

    def _load_sidecar_metric_rows(
        self,
        *,
        brands: tuple[BrandRef, ...],
        source: str,
        measure: str,
        dimension_filters: tuple[DimensionFilter, ...],
    ) -> list[dict]:
        mart_db = quote_identifier(self.mart_db)
        scope_sql, scope_params = brand_scope_predicate(brands)
        dimension_sql, dimension_params = dimension_filter_predicate(dimension_filters)
        rows = db.fetch_all(
            f"""
            SELECT brand_key, brand_name, atc4_code, product_code, dimension_type, raw_value_history
            FROM {mart_db}.{quote_identifier(FILTER_DIMENSION_TABLE)}
            WHERE source = %s
              AND measure = %s
              AND {scope_sql}
              AND ({dimension_sql})
            ORDER BY brand_key, atc4_code, product_code, dimension_type
            """,
            (source, measure, *scope_params, *dimension_params),
        )
        metadata = self._general_metadata(brands=brands, source=source, measure=measure)
        return sidecar_rows_to_metric_rows(
            rows,
            metadata=metadata,
            required_dimensions=tuple(item.dimension_type for item in dimension_filters),
        )

    def _general_metadata(
        self,
        *,
        brands: tuple[BrandRef, ...],
        source: str,
        measure: str,
    ) -> dict[tuple[str, str], dict]:
        mart_db = quote_identifier(self.mart_db)
        scope_sql, scope_params = brand_scope_predicate(brands)
        rows = db.fetch_all(
            f"""
            SELECT DISTINCT brand_key, atc4_code, atc4_desc, unit_label
            FROM {mart_db}.mart_general_brand_metric
            WHERE source = %s
              AND measure = %s
              AND {scope_sql}
            """,
            (source, measure, *scope_params),
        )
        return {(str(row["brand_key"]), str(row["atc4_code"])): row for row in rows}

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


def dimension_filter_predicate(filters: tuple[DimensionFilter, ...]) -> tuple[str, tuple[str, ...]]:
    """Return sidecar SQL implementing OR within each dimension."""

    parts: list[str] = []
    params: list[str] = []
    for item in filters:
        hashes = tuple(_dimension_value_hash(value) for value in item.values)
        if not hashes:
            continue
        parts.append(f"(dimension_type = %s AND dimension_value_hash IN ({placeholders(hashes)}))")
        params.append(item.dimension_type)
        params.extend(hashes)
    return " OR ".join(parts), tuple(params)


def placeholders(values: tuple[str, ...]) -> str:
    return ", ".join(["%s"] * len(values))


def sidecar_rows_to_metric_rows(
    rows: list[dict],
    *,
    metadata: dict[tuple[str, str], dict],
    required_dimensions: tuple[str, ...],
) -> list[dict]:
    """Collapse matching sidecar product rows into brand×ATC4 metric rows.

    The sidecar stores one metric history per product and dimension. A product
    that matches two requested dimensions therefore appears twice in SQL output;
    this function first proves that all requested dimensions matched, then uses
    one history per product so filtered product revenue is not double-counted.
    """

    products: dict[tuple[str, str, str], dict[str, object]] = {}
    required = set(required_dimensions)
    for row in rows:
        key = (str(row["brand_key"]), str(row["atc4_code"]), str(row["product_code"]))
        item = products.setdefault(
            key,
            {
                "brand_key": str(row["brand_key"]),
                "brand_name": str(row["brand_name"]),
                "atc4_code": str(row["atc4_code"]),
                "raw_value_history": row["raw_value_history"],
                "dimensions": set(),
            },
        )
        dimensions = item["dimensions"]
        if isinstance(dimensions, set):
            dimensions.add(str(row["dimension_type"]))

    histories_by_brand: dict[tuple[str, str, str], dict[str, float]] = {}
    for item in products.values():
        dimensions = item["dimensions"]
        if not isinstance(dimensions, set) or not required.issubset(dimensions):
            continue
        brand_key = str(item["brand_key"])
        row_key = (brand_key, str(item["brand_name"]), str(item["atc4_code"]))
        history = parse_history(str(item["raw_value_history"]))
        target = histories_by_brand.setdefault(row_key, {})
        for period, value in history.items():
            target[period] = target.get(period, 0.0) + value

    metric_rows: list[dict] = []
    for (brand_key, brand_name, atc4_code), history in sorted(histories_by_brand.items()):
        meta = metadata.get((brand_key, atc4_code), {})
        metric_rows.append(
            {
                "brand_key": brand_key,
                "brand_name": brand_name,
                "atc4_code": atc4_code,
                "atc4_desc": str(meta.get("atc4_desc") or ""),
                "unit_label": str(meta.get("unit_label") or ""),
                "raw_value_history": json.dumps(history, ensure_ascii=False, sort_keys=True),
            }
        )
    return metric_rows


def _dimension_value_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
    months = period_distance(first_period, last_period)
    if months <= 0:
        return None
    return round((math.pow(last_value / first_value, 12 / months) - 1) * 100, 6)


def period_distance(start: str, end: str) -> int:
    """Return elapsed months for monthly or quarterly period labels.

    UBIST histories use ``YYYY-MM`` month labels, while some IQVIA histories use
    ``YYYY-Qn`` quarter labels. Dynamic responses can still rank and size those
    markets without CAGR; for unknown labels this helper returns ``0`` so the
    caller omits CAGR instead of failing the whole API response.
    """

    start_index = period_to_month_index(start)
    end_index = period_to_month_index(end)
    if start_index is None or end_index is None:
        return 0
    return end_index - start_index


def period_to_month_index(period: str) -> int | None:
    if "-Q" in period:
        year_text, quarter_text = period.split("-Q", 1)
        try:
            year = int(year_text)
            quarter = int(quarter_text)
        except ValueError:
            return None
        if quarter < 1 or quarter > 4:
            return None
        return year * 12 + (quarter - 1) * 3 + 1
    if "-" in period:
        year_text, month_text = period.split("-", 1)
        try:
            year = int(year_text)
            month = int(month_text)
        except ValueError:
            return None
        if month < 1 or month > 12:
            return None
        return year * 12 + month
    return None


def month_distance(start: str, end: str) -> int:
    """Return elapsed month count between two ``YYYY-MM`` labels."""

    return period_distance(start, end)
