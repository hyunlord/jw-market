"""View-agnostic metric aggregation for dynamic markets."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
import hashlib
import json
import logging
import math
import re
from typing import Any

from pipeline.etl.io.mart.filter_dimension_metric import FILTER_DIMENSION_TABLE
from pipeline.etl.io.mart.strategic_filter_dimension_metric import STRATEGIC_DIMENSION_TABLE
from pipeline.scripts.api import db
from pipeline.scripts.api.competitor_ranking import MAX_COMPETITOR_COUNT, CompetitorRankItem, select_top_competitors
from pipeline.scripts.api.dynamic_market.channel_axis import (
    ChannelAxisFilter,
    history_from_audit_code_matrix,
    history_from_channel_specialty_matrix,
    parse_audit_code_matrix,
    parse_channel_specialty_matrix,
    slice_audit_code_matrix,
    slice_channel_specialty_matrix,
)
from pipeline.scripts.api.dynamic_market.types import (
    AggregatedMetrics,
    BrandMetric,
    BrandRef,
    DimensionFilter,
    PeriodRange,
    quote_identifier,
)
from pipeline.scripts.utils.ubist_channel_mapping import (
    UBIST_FACILITY_MAPPING,
    UBIST_SPECIALTY_MAPPING,
    parse_channel_code,
    raw_pair_to_channel_code,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _AggregatedRows:
    brand_metrics: list[BrandMetric]
    monthly_totals: dict[str, float]
    ranking_histories: dict[str, dict[str, float]]
    unit_label: str


@dataclass(frozen=True, slots=True)
class _UbistChannelSummary:
    specialty_channels: tuple[str, ...]
    specialty_target_channels: tuple[dict[str, Any], ...]


_ChannelCodeCache = dict[tuple[str, str], str | None]


def _filter_metric_pair_scope(
    rows: Iterable[dict[str, Any]],
    *,
    pair_scope: frozenset[tuple[str, str]],
    label: str,
) -> Iterable[dict[str, Any]]:
    filtered_rows = 0
    for row in rows:
        if pair_scope and (str(row["brand_key"]), str(row["atc4_code"])) not in pair_scope:
            filtered_rows += 1
            continue
        yield row
    logger.debug("%s_pair_filter filtered_rows=%s", label, filtered_rows)


@dataclass(frozen=True, slots=True)
class MetricAggregator:
    """Aggregate mart metric histories for a resolved brand set.

    The aggregator intentionally receives only brand keys plus source/measure.
    It does not know whether those keys came from the general resolver, a future
    strategic overlay resolver, or a predefined market.  This keeps ranking,
    HHI, CAGR, and monthly-series math reusable across views.
    """

    mart_db: str
    strategic_dimension_db: str | None = None

    def aggregate(
        self,
        *,
        brands: tuple[BrandRef, ...],
        source: str,
        measure: str,
        period_range: PeriodRange,
        top_n: int,
        dimension_filters: tuple[DimensionFilter, ...] = (),
        channel_axis: ChannelAxisFilter | None = None,
        view: str = "general",
        strategic_market_id: str | None = None,
        selected_brand_key: str | None = None,
    ) -> AggregatedMetrics:
        if not brands:
            return AggregatedMetrics(source, measure, "", 0.0, None, None, (), (), ())
        if view.startswith("strategic_"):
            if not strategic_market_id:
                raise ValueError("strategic aggregation requires strategic_market_id")
            if dimension_filters:
                rows = self._load_strategic_sidecar_metric_rows(
                    brands=brands,
                    source=source,
                    measure=measure,
                    dimension_filters=dimension_filters,
                    view=view,
                    strategic_market_id=strategic_market_id,
                )
            else:
                rows = self._load_strategic_mart_metric_rows(
                    brands=brands,
                    source=source,
                    measure=measure,
                    view=view,
                    strategic_market_id=strategic_market_id,
                )
        elif dimension_filters:
            rows = self._load_sidecar_metric_rows(
                brands=brands,
                source=source,
                measure=measure,
                dimension_filters=dimension_filters,
            )
        else:
            rows = self._iter_metric_rows(brands=brands, source=source, measure=measure, channel_axis=channel_axis)
        aggregated = self._aggregate_rows_detail(rows, period_range=period_range, channel_axis=channel_axis)
        market_size = float(sum(aggregated.monthly_totals.values()))
        rank_candidates = (
            _rank_general_brand_metrics(
                aggregated.brand_metrics,
                ranking_histories=aggregated.ranking_histories,
            )
            if view == "general"
            else sorted(aggregated.brand_metrics, key=lambda row: (-row.total_value, row.brand_key))
        )
        ranked = tuple(
            BrandMetric(
                brand_key=item.brand_key,
                brand_name=item.brand_name,
                atc4_code=item.atc4_code,
                total_value=item.total_value,
                market_share_pct=round((item.total_value / market_size) * 100, 6) if market_size > 0 else 0.0,
                rank=index,
                latest_period=item.latest_period,
                latest_value=item.latest_value,
                monthly_series=item.monthly_series,
                ubist_channel_by_display=item.ubist_channel_by_display,
                ubist_channel_by_code=item.ubist_channel_by_code,
                channel_specialty_matrix=item.channel_specialty_matrix,
                audit_code_matrix=item.audit_code_matrix,
                history_by_period=item.history_by_period,
                analysis_row=item.analysis_row,
            )
            for index, item in enumerate(rank_candidates, start=1)
        )
        monthly_series = tuple({"period": period, "market_size": value} for period, value in sorted(aggregated.monthly_totals.items()))
        ubist_summary = self._load_ubist_channel_summary(
            brand_metrics=aggregated.brand_metrics,
            source=source,
            channel_axis=channel_axis,
            view=view,
            dimension_filters=dimension_filters,
            latest_period=max(aggregated.monthly_totals) if aggregated.monthly_totals else None,
        )
        return AggregatedMetrics(
            source=source,
            measure=measure,
            unit_label=aggregated.unit_label,
            market_size=market_size,
            hhi=compute_hhi(ranked),
            cagr=compute_cagr(monthly_series),
            monthly_series=monthly_series,
            brands=select_top_competitors(
                tuple(CompetitorRankItem(item.brand_key, item.total_value, item) for item in ranked),
                selected_brand_key=selected_brand_key,
                top_n=MAX_COMPETITOR_COUNT,
            ),
            all_brands=ranked,
            ubist_specialty_channels=ubist_summary.specialty_channels,
            ubist_specialty_target_channels=ubist_summary.specialty_target_channels,
        )

    def _load_strategic_mart_metric_rows(
        self,
        *,
        brands: tuple[BrandRef, ...],
        source: str,
        measure: str,
        view: str,
        strategic_market_id: str,
    ) -> list[dict]:
        mart_db = quote_identifier(self.mart_db)
        scope_sql, scope_params = brand_scope_predicate(brands)
        table = strategic_table_for_view(view)
        id_column = "ml_id" if strategic_kind_for_view(view) == "ml" else "cd_market_id"
        return db.fetch_all(
            f"""
            SELECT brand_key, brand_name, '' AS atc4_code, source, measure, unit_label, raw_value_history,
                   by_dimension, dimension_data, dimension_channel_data, channel_data
            FROM {mart_db}.{table}
            WHERE {id_column} = %s
              AND source = %s
              AND measure = %s
              AND {scope_sql}
            ORDER BY brand_name, brand_key
            """,
            (strategic_market_id, source, measure, *scope_params),
        )

    def _load_strategic_sidecar_metric_rows(
        self,
        *,
        brands: tuple[BrandRef, ...],
        source: str,
        measure: str,
        dimension_filters: tuple[DimensionFilter, ...],
        view: str,
        strategic_market_id: str,
    ) -> list[dict]:
        dimension_db = quote_identifier(self.strategic_dimension_db or self.mart_db)
        scope_sql, scope_params = brand_scope_predicate(brands)
        dimension_sql, dimension_params = dimension_filter_predicate(dimension_filters, casefold_values=True)
        market_kind = strategic_kind_for_view(view)
        rows = db.fetch_all(
            f"""
            SELECT brand_key, brand_name, product_code, dimension_type, raw_value_history
            FROM {dimension_db}.{quote_identifier(STRATEGIC_DIMENSION_TABLE)}
            WHERE market_kind = %s
              AND market_id = %s
              AND source = %s
              AND measure = %s
              AND {scope_sql}
              AND ({dimension_sql})
            ORDER BY brand_key, product_code, dimension_type
            """,
            (market_kind, strategic_market_id, source, measure, *scope_params, *dimension_params),
        )
        metadata = self._strategic_metadata(
            brands=brands,
            source=source,
            measure=measure,
            view=view,
            strategic_market_id=strategic_market_id,
        )
        return strategic_sidecar_rows_to_metric_rows(
            rows,
            metadata=metadata,
            required_dimensions=tuple(item.dimension_type for item in dimension_filters),
        )

    def _strategic_metadata(
        self,
        *,
        brands: tuple[BrandRef, ...],
        source: str,
        measure: str,
        view: str,
        strategic_market_id: str,
    ) -> dict[str, dict]:
        mart_db = quote_identifier(self.mart_db)
        scope_sql, scope_params = brand_scope_predicate(brands)
        table = strategic_table_for_view(view)
        id_column = "ml_id" if strategic_kind_for_view(view) == "ml" else "cd_market_id"
        rows = db.fetch_all(
            f"""
            SELECT DISTINCT brand_key, unit_label
            FROM {mart_db}.{table}
            WHERE {id_column} = %s
              AND source = %s
              AND measure = %s
              AND {scope_sql}
            """,
            (strategic_market_id, source, measure, *scope_params),
        )
        return {str(row["brand_key"]): row for row in rows}

    def _iter_metric_rows(
        self,
        *,
        brands: tuple[BrandRef, ...],
        source: str,
        measure: str,
        channel_axis: ChannelAxisFilter | None,
    ) -> Iterable[dict[str, Any]]:
        mart_db = quote_identifier(self.mart_db)
        scope_sql, scope_params, pair_scope = brand_matrix_summary_scope(brands)
        sql = f"""
            SELECT brand_key, brand_name, atc4_code, source, measure, unit_label, raw_value_history,
                   by_dimension, channel_specialty_matrix, audit_code_matrix
            FROM {mart_db}.mart_general_brand_metric
            WHERE source = %s
              AND measure = %s
              AND {scope_sql}
            ORDER BY brand_name, brand_key
            """
        rows = _filter_metric_pair_scope(
            db.iter_rows(sql, (source, measure, *scope_params)),
            pair_scope=pair_scope,
            label="general_metric_rows",
        )
        return rows

    def _load_ubist_channel_summary(
        self,
        *,
        brand_metrics: list[BrandMetric],
        source: str,
        channel_axis: ChannelAxisFilter | None,
        view: str,
        dimension_filters: tuple[DimensionFilter, ...],
        latest_period: str | None,
    ) -> _UbistChannelSummary:
        if (
            view != "general"
            or dimension_filters
            or source != "ubist"
            or (channel_axis is not None and channel_axis.is_active)
            or not latest_period
        ):
            return _UbistChannelSummary((), ())
        totals_by_code: dict[str, float] = {}
        channel_code_cache: _ChannelCodeCache = {}
        for metric in brand_metrics:
            collect_ubist_channel_latest_totals(
                metric.channel_specialty_matrix,
                latest_period,
                totals_by_code,
                channel_code_cache=channel_code_cache,
            )
        if not totals_by_code:
            return _UbistChannelSummary((), ())
        ranked_codes = sorted(
            totals_by_code,
            key=lambda code: totals_by_code[code],
            reverse=True,
        )
        channels = []
        used: set[str] = set()
        for code in ranked_codes:
            if len(channels) >= 4:
                break
            parsed = parse_channel_code(code)
            if parsed is None or parsed.code in used:
                continue
            channels.append(parsed)
            used.add(parsed.code)
        if not channels:
            return _UbistChannelSummary((), ())
        return _UbistChannelSummary(
            specialty_channels=tuple(["전체", *[channel.display_name for channel in channels]]),
            specialty_target_channels=tuple(channel.as_dict() for channel in channels),
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
        dimension_sql, dimension_params = dimension_filter_predicate(dimension_filters, casefold_values=False)
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
            SELECT DISTINCT brand_key, atc4_code, unit_label, by_dimension, channel_specialty_matrix, audit_code_matrix
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
        rows: Iterable[Mapping[str, Any]],
        *,
        period_range: PeriodRange,
        channel_axis: ChannelAxisFilter | None = None,
    ) -> tuple[list[BrandMetric], dict[str, float]]:
        aggregated = self._aggregate_rows_detail(rows, period_range=period_range, channel_axis=channel_axis)
        return aggregated.brand_metrics, aggregated.monthly_totals

    def _aggregate_rows_detail(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        period_range: PeriodRange,
        channel_axis: ChannelAxisFilter | None = None,
    ) -> _AggregatedRows:
        brand_metrics: list[BrandMetric] = []
        monthly_totals: dict[str, float] = {}
        ranking_histories: dict[str, dict[str, float]] = {}
        incomplete_periods: set[str] = set()
        unit_label = ""
        for row in rows:
            if not unit_label:
                unit_label = str(row.get("unit_label") or "")
            raw_matrix = parse_channel_specialty_matrix(row.get("channel_specialty_matrix"))
            matrix = slice_channel_specialty_matrix(raw_matrix, channel_axis)
            window_matrix = _window_channel_specialty_matrix(matrix, period_range)
            raw_audit_matrix = parse_audit_code_matrix(row.get("audit_code_matrix"))
            audit_matrix = slice_audit_code_matrix(raw_audit_matrix, channel_axis)
            history = _history_for_row(
                raw_history=str(row["raw_value_history"]),
                channel_axis=channel_axis,
                channel_specialty_matrix=matrix,
                audit_code_matrix=audit_matrix,
            )
            incomplete_periods.update(period for period, value in history.items() if value is None)
            numeric_history = {period: value for period, value in history.items() if value is not None}
            brand_key = str(row["brand_key"])
            ranking_history = ranking_histories.setdefault(brand_key, {})
            for period, value in numeric_history.items():
                ranking_history[period] = ranking_history.get(period, 0.0) + value
            filtered = filter_periods(numeric_history, period_range)
            history_by_period = {period: value for period, value in sorted(filtered.items())}
            for period, value in filtered.items():
                monthly_totals[period] = monthly_totals.get(period, 0.0) + value
            latest_period = max(filtered) if filtered else None
            brand_metrics.append(
                BrandMetric(
                    brand_key=brand_key,
                    brand_name=str(row["brand_name"]),
                    atc4_code=str(row["atc4_code"]),
                    total_value=float(sum(filtered.values())),
                    market_share_pct=0.0,
                    rank=0,
                    latest_period=latest_period,
                    latest_value=filtered.get(latest_period) if latest_period else None,
                    monthly_series=tuple({"period": period, "value": value} for period, value in history_by_period.items()),
                    ubist_channel_by_display=parse_channel_series(row.get("ubist_channel_by_display")),
                    ubist_channel_by_code=parse_channel_series(row.get("ubist_channel_by_code")),
                    channel_specialty_matrix=window_matrix,
                    audit_code_matrix=audit_matrix,
                    history_by_period=history_by_period,
                    analysis_row=analysis_row_for_builder(row, history_by_period=history_by_period),
                )
            )
        if incomplete_periods:
            for period in incomplete_periods:
                monthly_totals.pop(period, None)
                for history in ranking_histories.values():
                    history.pop(period, None)
            brand_metrics = [_without_periods(metric, incomplete_periods) for metric in brand_metrics]
        return _AggregatedRows(
            brand_metrics=brand_metrics,
            monthly_totals=monthly_totals,
            ranking_histories=ranking_histories,
            unit_label=unit_label,
        )


def _history_for_row(
    *,
    raw_history: str,
    channel_axis: ChannelAxisFilter | None,
    channel_specialty_matrix: dict[str, dict[str, dict[str, float]]],
    audit_code_matrix: dict[str, dict[str, float]],
) -> dict[str, float | None]:
    if channel_axis is None or not channel_axis.is_active:
        return parse_history(raw_history)
    if channel_axis.source == "ubist":
        return history_from_channel_specialty_matrix(channel_specialty_matrix)
    if channel_axis.source == "iqvia_nsa":
        return history_from_audit_code_matrix(audit_code_matrix)
    return parse_history(raw_history)


def _without_periods(metric: BrandMetric, excluded: set[str]) -> BrandMetric:
    history = {period: value for period, value in metric.history_by_period.items() if period not in excluded}
    latest_period = max(history) if history else None
    analysis_row = dict(metric.analysis_row)
    metric_history = analysis_row.get("metric_history")
    if isinstance(metric_history, dict):
        analysis_row["metric_history"] = {
            period: value
            for period, value in metric_history.items()
            if period not in excluded
        }
    return replace(
        metric,
        total_value=float(sum(history.values())),
        latest_period=latest_period,
        latest_value=history.get(latest_period) if latest_period else None,
        monthly_series=tuple({"period": period, "value": value} for period, value in history.items()),
        history_by_period=history,
        analysis_row=analysis_row,
    )


def _rank_general_brand_metrics(
    brand_metrics: Iterable[BrandMetric],
    *,
    ranking_histories: Mapping[str, Mapping[str, float]],
) -> list[BrandMetric]:
    metrics = list(brand_metrics)
    indexed_periods = [
        (index, period)
        for history_by_period in ranking_histories.values()
        for period in history_by_period
        if (index := period_to_month_index(period)) is not None
    ]
    if not indexed_periods:
        return sorted(metrics, key=lambda row: (-row.total_value, row.brand_key))

    _, latest_period = max(indexed_periods)
    latest_value_by_brand = {
        brand_key: history_by_period.get(latest_period, 0.0)
        for brand_key, history_by_period in ranking_histories.items()
    }

    def rank_key(metric: BrandMetric) -> tuple[int, float, float, str]:
        latest_value = latest_value_by_brand.get(metric.brand_key, 0.0)
        return (
            0 if latest_value > 0 else 1,
            -latest_value if latest_value > 0 else 0.0,
            -metric.total_value,
            metric.brand_key,
        )

    return sorted(metrics, key=rank_key)


def analysis_row_for_builder(row: Mapping[str, Any], *, history_by_period: Mapping[str, float]) -> dict[str, Any]:
    """Return the mart-shaped row expected by the cache-cause level builders."""

    return {
        "brand_key": row.get("brand_key"),
        "brand_name": row.get("brand_name"),
        "atc4_code": row.get("atc4_code"),
        "source": row.get("source"),
        "measure": row.get("measure"),
        "unit_label": row.get("unit_label"),
        "by_dimension": row.get("by_dimension"),
        "dimension_data": row.get("dimension_data"),
        "dimension_channel_data": row.get("dimension_channel_data"),
        "dimension_specialty_data": row.get("dimension_specialty_data"),
        "channel_data": row.get("channel_data"),
        "channel_specialty_matrix": row.get("channel_specialty_matrix"),
        "metric_history": {
            str(period): {"raw_value": float(value or 0.0)}
            for period, value in history_by_period.items()
        },
    }


def merge_json_object(existing: Any, extra: Any) -> str:
    merged = _json_object(existing)
    for key, value in _json_object(extra).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return json.dumps(merged, ensure_ascii=False, sort_keys=True)


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        payload = json.loads(raw)
        return dict(payload) if isinstance(payload, dict) else {}
    return {}


def collect_ubist_channel_totals(
    raw: Any,
    totals_by_code_period: dict[str, dict[str, float]],
    *,
    channel_code_cache: _ChannelCodeCache | None = None,
) -> None:
    if isinstance(raw, dict):
        payload = raw
    elif isinstance(raw, str) and raw.strip():
        payload = json.loads(raw)
    else:
        return
    code_cache = channel_code_cache if channel_code_cache is not None else {}
    if not isinstance(payload, dict):
        return
    for facility, specialties in payload.items():
        if not isinstance(specialties, dict):
            continue
        for specialty, series in specialties.items():
            if not isinstance(series, dict):
                continue
            code = _cached_raw_pair_to_channel_code(facility, specialty, code_cache)
            if not code:
                continue
            target = totals_by_code_period.setdefault(code, {})
            for period, value in series.items():
                period_key = str(period)
                target[period_key] = target.get(period_key, 0.0) + float(value or 0.0)


def collect_ubist_channel_latest_totals(
    raw: Any,
    latest_period: str,
    totals_by_code: dict[str, float],
    *,
    channel_code_cache: _ChannelCodeCache | None = None,
) -> None:
    code_cache = channel_code_cache if channel_code_cache is not None else {}
    if isinstance(raw, dict):
        payload = raw
    elif isinstance(raw, str) and raw.strip():
        payload = json.loads(raw)
    else:
        return
    if not isinstance(payload, dict):
        return
    for facility, specialties in payload.items():
        if not isinstance(specialties, dict):
            continue
        for specialty, series in specialties.items():
            if not isinstance(series, dict):
                continue
            value = series.get(latest_period)
            if value is None:
                continue
            code = _cached_raw_pair_to_channel_code(facility, specialty, code_cache)
            if not code:
                continue
            totals_by_code[code] = totals_by_code.get(code, 0.0) + float(value or 0.0)


def _cached_raw_pair_to_channel_code(
    facility_raw: Any,
    specialty_raw: Any,
    cache: _ChannelCodeCache,
) -> str | None:
    key = (str(facility_raw or "").strip(), str(specialty_raw or "").strip())
    if key not in cache:
        cache[key] = raw_pair_to_channel_code(key[0], key[1])
    return cache[key]


def parse_history(raw: str) -> dict[str, float | None]:
    """Parse mart JSON history into month -> numeric value."""

    payload = json.loads(raw)
    return {
        str(period): None if value is None else float(value)
        for period, value in payload.items()
    }


def parse_channel_series(raw: object) -> dict[str, dict[str, float]]:
    """Parse optional UBIST mart channel JSON into channel -> period -> value."""

    if isinstance(raw, dict):
        payload = raw
    elif isinstance(raw, str) and raw.strip():
        payload = json.loads(raw)
    else:
        return {}
    if not isinstance(payload, dict):
        return {}
    parsed: dict[str, dict[str, float]] = {}
    for channel, series in payload.items():
        if not isinstance(series, dict):
            continue
        parsed[str(channel)] = {str(period): float(value or 0.0) for period, value in series.items()}
    return parsed


def filter_periods(history: dict[str, float], period_range: PeriodRange) -> dict[str, float]:
    """Apply an inclusive ``YYYY-MM`` range to a metric history."""

    return {
        period: value
        for period, value in history.items()
        if (period_range.start is None or period >= period_range.start)
        and (period_range.end is None or period <= period_range.end)
    }


def _window_channel_specialty_matrix(
    matrix: dict[str, dict[str, dict[str, float]]],
    period_range: PeriodRange,
) -> dict[str, dict[str, dict[str, float]]]:
    if period_range.start is None and period_range.end is None:
        return matrix
    return {
        facility: {
            specialty: filter_periods(series, period_range)
            for specialty, series in specialties.items()
        }
        for facility, specialties in matrix.items()
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
        predicates = ", ".join(["(%s, %s)"] * len(pairs))
        params = tuple(value for pair in pairs for value in pair)
        return f"(brand_key, atc4_code) IN ({predicates})", params

    brand_keys = tuple(brand.brand_key for brand in brands)
    placeholders = ", ".join(["%s"] * len(brand_keys))
    return f"brand_key IN ({placeholders})", brand_keys


def brand_matrix_summary_scope(brands: tuple[BrandRef, ...]) -> tuple[str, tuple[str, ...], frozenset[tuple[str, str]]]:
    """Return a matrix-summary scope that avoids tuple predicates on LONGTEXT rows."""

    if all(brand.atc4_code for brand in brands):
        brand_keys = tuple(dict.fromkeys(brand.brand_key for brand in brands))
        atc4_codes = tuple(dict.fromkeys(brand.atc4_code for brand in brands))
        pair_scope = frozenset((brand.brand_key, brand.atc4_code) for brand in brands)
        return (
            f"brand_key IN ({placeholders(brand_keys)}) AND atc4_code IN ({placeholders(atc4_codes)})",
            (*brand_keys, *atc4_codes),
            pair_scope,
        )

    scope_sql, scope_params = brand_scope_predicate(brands)
    return scope_sql, scope_params, frozenset()


def dimension_filter_predicate(
    filters: tuple[DimensionFilter, ...],
    *,
    casefold_values: bool = False,
) -> tuple[str, tuple[str, ...]]:
    """Return sidecar SQL implementing OR within each dimension."""

    parts: list[str] = []
    params: list[str] = []
    for item in filters:
        hashes = tuple(_dimension_value_hash(value, casefold=casefold_values) for value in item.values)
        if not hashes:
            continue
        parts.append(f"(dimension_type = %s AND dimension_value_hash IN ({placeholders(hashes)}))")
        params.append(item.dimension_type)
        params.extend(hashes)
    return " OR ".join(parts), tuple(params)


def placeholders(values: tuple[str, ...]) -> str:
    return ", ".join(["%s"] * len(values))


def _sum_history_value(target: dict[str, float | None], period: str, value: float | None) -> None:
    if value is None or target.get(period) is None and period in target:
        target[period] = None
        return
    target[period] = float(target.get(period, 0.0) or 0.0) + value


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

    histories_by_brand: dict[tuple[str, str, str], dict[str, float | None]] = {}
    for item in products.values():
        dimensions = item["dimensions"]
        if not isinstance(dimensions, set) or not required.issubset(dimensions):
            continue
        brand_key = str(item["brand_key"])
        row_key = (brand_key, str(item["brand_name"]), str(item["atc4_code"]))
        history = parse_history(str(item["raw_value_history"]))
        target = histories_by_brand.setdefault(row_key, {})
        for period, value in history.items():
            _sum_history_value(target, period, value)

    metric_rows: list[dict] = []
    for (brand_key, brand_name, atc4_code), history in sorted(histories_by_brand.items()):
        meta = metadata.get((brand_key, atc4_code), {})
        metric_row = {
            "brand_key": brand_key,
            "brand_name": brand_name,
            "atc4_code": atc4_code,
            "unit_label": str(meta.get("unit_label") or ""),
            "raw_value_history": json.dumps(history, ensure_ascii=False, sort_keys=True),
            "channel_specialty_matrix": meta.get("channel_specialty_matrix") or {},
        }
        if meta.get("by_dimension"):
            metric_row["by_dimension"] = meta["by_dimension"]
        if meta.get("audit_code_matrix"):
            metric_row["audit_code_matrix"] = meta["audit_code_matrix"]
        metric_rows.append(metric_row)
    return metric_rows


def strategic_sidecar_rows_to_metric_rows(
    rows: list[dict],
    *,
    metadata: dict[str, dict],
    required_dimensions: tuple[str, ...],
) -> list[dict]:
    products: dict[tuple[str, str], dict[str, object]] = {}
    required = set(required_dimensions)
    for row in rows:
        key = (str(row["brand_key"]), str(row["product_code"]))
        item = products.setdefault(
            key,
            {
                "brand_key": str(row["brand_key"]),
                "brand_name": str(row["brand_name"]),
                "raw_value_history": row["raw_value_history"],
                "dimensions": set(),
            },
        )
        dimensions = item["dimensions"]
        if isinstance(dimensions, set):
            dimensions.add(str(row["dimension_type"]))

    histories_by_brand: dict[tuple[str, str], dict[str, float | None]] = {}
    for item in products.values():
        dimensions = item["dimensions"]
        if not isinstance(dimensions, set) or not required.issubset(dimensions):
            continue
        brand_key = str(item["brand_key"])
        row_key = (brand_key, str(item["brand_name"]))
        history = parse_history(str(item["raw_value_history"]))
        target = histories_by_brand.setdefault(row_key, {})
        for period, value in history.items():
            _sum_history_value(target, period, value)

    metric_rows: list[dict] = []
    for (brand_key, brand_name), history in sorted(histories_by_brand.items()):
        meta = metadata.get(brand_key, {})
        metric_rows.append(
            {
                "brand_key": brand_key,
                "brand_name": brand_name,
                "atc4_code": "",
                "unit_label": str(meta.get("unit_label") or ""),
                "raw_value_history": json.dumps(history, ensure_ascii=False, sort_keys=True),
            }
        )
    return metric_rows


def strategic_kind_for_view(view: str) -> str:
    if view == "strategic_ml":
        return "ml"
    if view == "strategic_cd":
        return "cd"
    raise ValueError(f"unsupported strategic view: {view}")


def strategic_table_for_view(view: str) -> str:
    return "mart_strategic_ml_brand_metric" if strategic_kind_for_view(view) == "ml" else "mart_strategic_cd_brand_metric"


def _dimension_value_hash(value: str, *, casefold: bool = False) -> str:
    normalized = re.sub(r"\s+", " ", value.strip()).casefold()
    if not casefold:
        normalized = re.sub(r"\s+", " ", value.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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
    """Return elapsed months between supported month or quarter labels."""

    return period_distance(start, end)
