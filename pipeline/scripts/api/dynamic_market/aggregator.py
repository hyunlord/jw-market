"""View-agnostic metric aggregation for dynamic markets."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import re
from json.decoder import scanstring
from typing import Any

from pipeline.etl.io.mart.filter_dimension_metric import FILTER_DIMENSION_TABLE
from pipeline.etl.io.mart.strategic_filter_dimension_metric import STRATEGIC_DIMENSION_TABLE
from pipeline.scripts.api import db
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


@dataclass(frozen=True, slots=True)
class _AggregatedRows:
    brand_metrics: list[BrandMetric]
    monthly_totals: dict[str, float]
    unit_label: str


@dataclass(frozen=True, slots=True)
class _UbistChannelSummary:
    specialty_channels: tuple[str, ...]
    specialty_target_channels: tuple[dict[str, Any], ...]


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
            )
            for index, item in enumerate(
                sorted(aggregated.brand_metrics, key=lambda row: (-row.total_value, row.brand_key)),
                start=1,
            )
        )
        monthly_series = tuple({"period": period, "market_size": value} for period, value in sorted(aggregated.monthly_totals.items()))
        ubist_summary = self._load_ubist_channel_summary(
            brands=brands,
            source=source,
            measure=measure,
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
            brands=ranked[:top_n],
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
            SELECT brand_key, brand_name, '' AS atc4_code, source, measure, unit_label, raw_value_history
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
        dimension_sql, dimension_params = dimension_filter_predicate(dimension_filters)
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
        scope_sql, scope_params = brand_scope_predicate(brands)
        extra_columns = general_metric_extra_columns(channel_axis=channel_axis)
        sql = f"""
            SELECT brand_key, brand_name, atc4_code, source, measure, unit_label, raw_value_history{extra_columns}
            FROM {mart_db}.mart_general_brand_metric
            WHERE source = %s
              AND measure = %s
              AND {scope_sql}
            ORDER BY brand_name, brand_key
            """
        return db.iter_rows(
            sql,
            (source, measure, *scope_params),
        )

    def _load_ubist_channel_summary(
        self,
        *,
        brands: tuple[BrandRef, ...],
        source: str,
        measure: str,
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
        mart_db = quote_identifier(self.mart_db)
        scope_sql, scope_params = brand_scope_predicate(brands)
        totals_by_code: dict[str, float] = {}
        rows = db.iter_rows(
            f"""
            SELECT channel_specialty_matrix
            FROM {mart_db}.mart_general_brand_metric
            WHERE source = %s
              AND measure = %s
              AND {scope_sql}
            ORDER BY brand_name, brand_key
            """,
            (source, measure, *scope_params),
        )
        for row in rows:
            collect_ubist_channel_latest_totals(row.get("channel_specialty_matrix"), latest_period, totals_by_code)
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
            SELECT DISTINCT brand_key, atc4_code, unit_label, channel_specialty_matrix, audit_code_matrix
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
        unit_label = ""
        for row in rows:
            if not unit_label:
                unit_label = str(row.get("unit_label") or "")
            raw_matrix = parse_channel_specialty_matrix(row.get("channel_specialty_matrix"))
            matrix = slice_channel_specialty_matrix(raw_matrix, channel_axis)
            raw_audit_matrix = parse_audit_code_matrix(row.get("audit_code_matrix"))
            audit_matrix = slice_audit_code_matrix(raw_audit_matrix, channel_axis)
            history = _history_for_row(
                raw_history=str(row["raw_value_history"]),
                channel_axis=channel_axis,
                channel_specialty_matrix=matrix,
                audit_code_matrix=audit_matrix,
            )
            filtered = filter_periods(history, period_range)
            for period, value in filtered.items():
                monthly_totals[period] = monthly_totals.get(period, 0.0) + value
            latest_period = max(filtered) if filtered else None
            brand_metrics.append(
                BrandMetric(
                    brand_key=str(row["brand_key"]),
                    brand_name=str(row["brand_name"]),
                    atc4_code=str(row["atc4_code"]),
                    total_value=float(sum(filtered.values())),
                    market_share_pct=0.0,
                    rank=0,
                    latest_period=latest_period,
                    latest_value=filtered.get(latest_period) if latest_period else None,
                    monthly_series=tuple({"period": period, "value": value} for period, value in sorted(filtered.items())),
                    ubist_channel_by_display=parse_channel_series(row.get("ubist_channel_by_display")),
                    ubist_channel_by_code=parse_channel_series(row.get("ubist_channel_by_code")),
                    channel_specialty_matrix=matrix,
                    audit_code_matrix=audit_matrix,
                )
            )
        return _AggregatedRows(brand_metrics=brand_metrics, monthly_totals=monthly_totals, unit_label=unit_label)


def _history_for_row(
    *,
    raw_history: str,
    channel_axis: ChannelAxisFilter | None,
    channel_specialty_matrix: dict[str, dict[str, dict[str, float]]],
    audit_code_matrix: dict[str, dict[str, float]],
) -> dict[str, float]:
    if channel_axis is None or not channel_axis.is_active:
        return parse_history(raw_history)
    if channel_axis.source == "ubist":
        return history_from_channel_specialty_matrix(channel_specialty_matrix)
    if channel_axis.source == "iqvia_nsa":
        return history_from_audit_code_matrix(audit_code_matrix)
    return parse_history(raw_history)


def general_metric_extra_columns(*, channel_axis: ChannelAxisFilter | None) -> str:
    if channel_axis is None or not channel_axis.is_active:
        return ""
    if channel_axis.source == "ubist":
        return ", channel_specialty_matrix"
    if channel_axis.source == "iqvia_nsa":
        return ", audit_code_matrix"
    return ""


def collect_ubist_channel_totals(raw: Any, totals_by_code_period: dict[str, dict[str, float]]) -> None:
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
            code = raw_pair_to_channel_code(facility, specialty)
            if not code:
                continue
            target = totals_by_code_period.setdefault(code, {})
            for period, value in series.items():
                period_key = str(period)
                target[period_key] = target.get(period_key, 0.0) + float(value or 0.0)


def collect_ubist_channel_latest_totals(raw: Any, latest_period: str, totals_by_code: dict[str, float]) -> None:
    if isinstance(raw, dict):
        payload = raw
    elif isinstance(raw, str) and raw.strip():
        if collect_ubist_channel_latest_totals_from_json(raw, latest_period, totals_by_code):
            return
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
            code = raw_pair_to_channel_code(facility, specialty)
            if not code:
                continue
            totals_by_code[code] = totals_by_code.get(code, 0.0) + float(value or 0.0)


_JSON_NUMBER_RE = re.compile(r"\s*(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|true|false|null)")
_RAW_FACILITY_VALUES = frozenset(str(raw) for meta in UBIST_FACILITY_MAPPING.values() for raw in meta["raw_values"])
_RAW_CHANNEL_PAIRS: tuple[tuple[str, str, str], ...] = tuple(
    dict.fromkeys(
        (str(facility), str(specialty), code)
        for facility_meta in UBIST_FACILITY_MAPPING.values()
        for facility in facility_meta["raw_values"]
        for specialty in {
            str(raw_value)
            for specialty_meta in UBIST_SPECIALTY_MAPPING.values()
            for raw_value in specialty_meta["raw_values"]
        }
        for code in (raw_pair_to_channel_code(facility, specialty),)
        if code
    )
)


def collect_ubist_channel_latest_totals_from_json(raw: str, latest_period: str, totals_by_code: dict[str, float]) -> bool:
    """Collect latest-period UBIST channel totals without materializing matrix JSON.

    This preserves the existing attribution rule by using the same
    ``raw_pair_to_channel_code`` pair set as the full parser. It intentionally
    extracts only one period because general dynamic responses only need the
    latest top-channel labels.
    """

    text = raw.strip()
    if not text or text[0] != "{":
        return False
    try:
        _collect_ubist_matrix_object(text, 0, latest_period, totals_by_code)
    except ValueError:
        return False
    return True


def _collect_ubist_matrix_object(text: str, pos: int, latest_period: str, totals_by_code: dict[str, float]) -> int:
    pos = _expect_char(text, _skip_ws(text, pos), "{") + 1
    while True:
        pos = _skip_ws(text, pos)
        if pos >= len(text):
            raise ValueError("unterminated matrix object")
        if text[pos] == "}":
            return pos + 1
        facility, pos = _read_json_key(text, pos)
        pos = _expect_char(text, _skip_ws(text, pos), ":") + 1
        pos = _skip_ws(text, pos)
        if facility in _RAW_FACILITY_VALUES and pos < len(text) and text[pos] == "{":
            pos = _collect_facility_object(text, pos, facility, latest_period, totals_by_code)
        else:
            pos = _skip_json_value(text, pos)
        pos = _consume_member_separator(text, pos)


def _collect_facility_object(
    text: str,
    pos: int,
    facility: str,
    latest_period: str,
    totals_by_code: dict[str, float],
) -> int:
    pos = _expect_char(text, _skip_ws(text, pos), "{") + 1
    while True:
        pos = _skip_ws(text, pos)
        if pos >= len(text):
            raise ValueError("unterminated facility object")
        if text[pos] == "}":
            return pos + 1
        specialty, pos = _read_json_key(text, pos)
        pos = _expect_char(text, _skip_ws(text, pos), ":") + 1
        pos = _skip_ws(text, pos)
        code = raw_pair_to_channel_code(facility, specialty)
        if code and pos < len(text) and text[pos] == "{":
            value, pos = _read_latest_period_value(text, pos, latest_period)
            if value is not None:
                totals_by_code[code] = totals_by_code.get(code, 0.0) + value
        else:
            pos = _skip_json_value(text, pos)
        pos = _consume_member_separator(text, pos)


def _read_latest_period_value(text: str, pos: int, latest_period: str) -> tuple[float | None, int]:
    latest_value: float | None = None
    pos = _expect_char(text, _skip_ws(text, pos), "{") + 1
    while True:
        pos = _skip_ws(text, pos)
        if pos >= len(text):
            raise ValueError("unterminated period object")
        if text[pos] == "}":
            return latest_value, pos + 1
        period, pos = _read_json_key(text, pos)
        pos = _expect_char(text, _skip_ws(text, pos), ":") + 1
        value, pos = _read_json_scalar(text, pos)
        if period == latest_period:
            latest_value = value
        pos = _consume_member_separator(text, pos)


def _read_json_key(text: str, pos: int) -> tuple[str, int]:
    pos = _skip_ws(text, pos)
    if pos >= len(text) or text[pos] != '"':
        raise ValueError("expected string key")
    value, end = scanstring(text, pos + 1, True)
    return str(value), end + 1


def _read_json_scalar(text: str, pos: int) -> tuple[float | None, int]:
    match = _JSON_NUMBER_RE.match(text, pos)
    if match is None:
        raise ValueError("expected scalar")
    token = match.group(1)
    end = match.end()
    if token in {"null", "true", "false"}:
        return None, end
    return float(token), end


def _skip_json_value(text: str, pos: int) -> int:
    pos = _skip_ws(text, pos)
    if pos >= len(text):
        raise ValueError("missing value")
    if text[pos] == '"':
        _, end = scanstring(text, pos + 1, True)
        return end + 1
    if text[pos] == "{":
        return _skip_json_container(text, pos, "{", "}")
    if text[pos] == "[":
        return _skip_json_container(text, pos, "[", "]")
    match = _JSON_NUMBER_RE.match(text, pos)
    if match is None:
        raise ValueError("unknown value")
    return match.end()


def _skip_json_container(text: str, pos: int, open_char: str, close_char: str) -> int:
    pos = _expect_char(text, pos, open_char)
    depth = 0
    in_string = False
    escaped = False
    for index in range(pos, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return index + 1
    raise ValueError("unterminated container")


def _consume_member_separator(text: str, pos: int) -> int:
    pos = _skip_ws(text, pos)
    if pos < len(text) and text[pos] == ",":
        return pos + 1
    return pos


def _expect_char(text: str, pos: int, expected: str) -> int:
    if pos >= len(text) or text[pos] != expected:
        raise ValueError(f"expected {expected!r}")
    return pos


def _skip_ws(text: str, pos: int) -> int:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def parse_history(raw: str) -> dict[str, float]:
    """Parse mart JSON history into month -> numeric value."""

    payload = json.loads(raw)
    return {str(period): float(value or 0.0) for period, value in payload.items()}


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
        metric_row = {
            "brand_key": brand_key,
            "brand_name": brand_name,
            "atc4_code": atc4_code,
            "unit_label": str(meta.get("unit_label") or ""),
            "raw_value_history": json.dumps(history, ensure_ascii=False, sort_keys=True),
            "channel_specialty_matrix": meta.get("channel_specialty_matrix") or {},
        }
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

    histories_by_brand: dict[tuple[str, str], dict[str, float]] = {}
    for item in products.values():
        dimensions = item["dimensions"]
        if not isinstance(dimensions, set) or not required.issubset(dimensions):
            continue
        brand_key = str(item["brand_key"])
        row_key = (brand_key, str(item["brand_name"]))
        history = parse_history(str(item["raw_value_history"]))
        target = histories_by_brand.setdefault(row_key, {})
        for period, value in history.items():
            target[period] = target.get(period, 0.0) + value

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
