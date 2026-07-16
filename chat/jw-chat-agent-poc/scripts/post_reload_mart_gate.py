#!/usr/bin/env python3
"""Fail closed when a mart reload is stale, incomplete, or internally inflated.

This gate intentionally derives expectations from independent mart headline rows.
It does not own fixed market values: a legitimate reload may change every observed
dimension total while the parity contracts remain invariant.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence
import unicodedata


ABS_TOLERANCE = 0.01
SIDECAR_DIMENSION_TYPES = (
    "seller",
    "molecule_strength",
    "form",
    "route",
    "reimbursement",
)
BLOCK_LEVEL_BY_DIMENSION = {
    "seller": "판매사",
    "molecule": "성분",
    "molecule_strength": "성분용량",
    "form": "제형",
    "route": "투여경로",
    "reimbursement": "급여구분",
}
EXPECTED_SOURCE_TABLES = {
    "general_brand": "mart_general_brand_metric",
    "general_market": "mart_general_market_metric",
    "general_dimension": "mart_general_filter_dimension_metric",
    "strategic_brand": "mart_strategic_ml_brand_metric",
    "strategic_market": "mart_strategic_ml_market_metric",
}
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        stripped = value.strip()
        return json.loads(stripped) if stripped else {}
    return value


def _json_object(value: Any) -> dict[str, Any]:
    parsed = _json_value(value)
    return dict(parsed) if isinstance(parsed, dict) else {}


def _point_value(point: Any) -> float | None:
    if isinstance(point, Mapping):
        point = next(
            (
                point[key]
                for key in ("raw_value", "value", "market_size", "total", "sales")
                if point.get(key) is not None
            ),
            None,
        )
    if point is None:
        return None
    try:
        value = float(point)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _series_map(value: Any) -> dict[str, float | None]:
    parsed = _json_value(value)
    if isinstance(parsed, Mapping):
        return {str(period): _point_value(point) for period, point in sorted(parsed.items())}
    if isinstance(parsed, list):
        result: dict[str, float | None] = {}
        for point in parsed:
            if not isinstance(point, Mapping) or not point.get("period"):
                continue
            result[str(point["period"])] = _point_value(point)
        return dict(sorted(result.items()))
    return {}


def _history_value(value: Any, period: str) -> float | None:
    return _series_map(value).get(period)


def _close(actual: float, expected: float) -> bool:
    return math.isclose(actual, expected, rel_tol=0.0, abs_tol=ABS_TOLERANCE)


def _utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        text = str(value).strip().replace(" ", "T")
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalized_label(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def _quote_identifier(value: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"unsafe SQL identifier: {value!r}")
    return f"`{value}`"


def _gate(
    name: str,
    *,
    checked: int,
    population: int,
    failures: Sequence[str],
    tolerance: str,
) -> dict[str, Any]:
    failure_list = list(failures)
    exit_code = 1 if population == 0 or checked != population or failure_list else 0
    return {
        "gate": name,
        "classification": "census",
        "checked": checked,
        "population": population,
        "missing": "fail",
        "tolerance": tolerance,
        "failures": failure_list,
        "failure_count": len(failure_list),
        "exit_code": exit_code,
        "environment": "runtime_mart_read_only",
    }


def _market_series(rows: Iterable[Mapping[str, Any]]) -> tuple[dict[str, dict[str, float]], list[str]]:
    totals: dict[str, dict[str, float]] = {}
    failures: list[str] = []
    for row in rows:
        market = str(row.get("market_id") or "").strip()
        series = {
            period: amount
            for period, amount in _series_map(row.get("market_size_series")).items()
            if amount is not None
        }
        if not market or not series:
            failures.append(f"market_headline_missing:{market or '<empty>'}")
            continue
        if market in totals:
            failures.append(f"market_headline_duplicate:{market}")
            continue
        totals[market] = series
    return totals, failures


def _validate_freshness(evidence: Mapping[str, Any]) -> dict[str, Any]:
    cohort = _json_object(evidence.get("cohort"))
    block_rows = list(evidence.get("block_rows") or [])
    source_tables = list(evidence.get("source_tables") or [])
    failures: list[str] = []
    if int(evidence.get("tx_read_only") or 0) != 1:
        failures.append("transaction_is_not_read_only")
    epoch = str(cohort.get("source_epoch") or "")
    build_version = str(cohort.get("build_version") or "")
    if not epoch or not build_version:
        failures.append("latest_block_cohort_identity_missing")
    for row in block_rows:
        if str(row.get("source_epoch") or "") != epoch:
            failures.append(f"mixed_source_epoch:{row.get('market_id')}")
        if str(row.get("build_version") or "") != build_version:
            failures.append(f"mixed_build_version:{row.get('market_id')}")
    cohort_row_count = int(cohort.get("row_count") or 0)
    if cohort_row_count != len(block_rows):
        failures.append(f"cohort_row_count_mismatch:cohort={cohort_row_count}:rows={len(block_rows)}")
    by_logical: dict[str, Mapping[str, Any]] = {}
    for row in source_tables:
        logical_name = str(row.get("logical_name") or "")
        if not logical_name or logical_name in by_logical:
            failures.append(f"source_table_identity_duplicate:{logical_name or '<empty>'}")
            continue
        by_logical[logical_name] = row
    missing_sources = sorted(set(EXPECTED_SOURCE_TABLES) - set(by_logical))
    extra_sources = sorted(set(by_logical) - set(EXPECTED_SOURCE_TABLES))
    if missing_sources or extra_sources:
        failures.append(f"source_table_coverage_mismatch:missing={missing_sources}:extra={extra_sources}")
    for logical_name, table_name in EXPECTED_SOURCE_TABLES.items():
        row = by_logical.get(logical_name)
        if row is None:
            continue
        if str(row.get("table_name") or "") != table_name:
            failures.append(
                f"source_table_name_mismatch:{logical_name}:actual={row.get('table_name')}:expected={table_name}"
            )
        if int(row.get("row_count") or 0) <= 0:
            failures.append(f"source_table_population_empty:{logical_name}")
        computed_min = _utc_datetime(row.get("computed_at_min"))
        computed_max = _utc_datetime(row.get("computed_at_max"))
        if computed_min is None or computed_max is None:
            failures.append(f"source_table_timestamp_missing:{logical_name}")
        elif computed_min > computed_max:
            failures.append(f"source_table_timestamp_inverted:{logical_name}")
    built_at = _utc_datetime(cohort.get("built_at_min"))
    source_times = [
        _utc_datetime(by_logical[name].get("computed_at_max"))
        for name in EXPECTED_SOURCE_TABLES
        if name in by_logical
    ]
    source_at = max((value for value in source_times if value is not None), default=None)
    if built_at is None or source_at is None:
        failures.append("cohort_or_source_timestamp_missing")
    elif built_at < source_at:
        failures.append(
            f"analysis_block_precedes_source:built={built_at.isoformat()}:source={source_at.isoformat()}"
        )
    markets = {str(row.get("market_id") or "") for row in evidence.get("market_rows") or []}
    block_markets = {str(row.get("market_id") or "") for row in block_rows}
    if markets != block_markets:
        failures.append(
            f"block_market_coverage_mismatch:missing={sorted(markets - block_markets)}:extra={sorted(block_markets - markets)}"
        )
    return _gate(
        "source_epoch_freshness",
        checked=len(EXPECTED_SOURCE_TABLES) + len(block_rows) + 5,
        population=len(EXPECTED_SOURCE_TABLES) + len(block_rows) + 5,
        failures=failures,
        tolerance="exact",
    )


def _validate_dimensions(evidence: Mapping[str, Any]) -> dict[str, Any]:
    market_totals, failures = _market_series(evidence.get("market_rows") or [])
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in evidence.get("dimension_rows") or []:
        grouped[(str(row.get("market_id") or ""), str(row.get("dimension_type") or ""))].append(row)
    population = sum(len(periods) * len(SIDECAR_DIMENSION_TYPES) for periods in market_totals.values())
    checked = 0
    for market, periods in market_totals.items():
        for dimension in SIDECAR_DIMENSION_TYPES:
            rows = grouped.get((market, dimension), [])
            if not rows:
                failures.append(f"dimension_population_missing:{market}:{dimension}")
                continue
            for period, expected in periods.items():
                values = [_history_value(row.get("raw_value_history"), period) for row in rows]
                if any(value is None for value in values):
                    failures.append(f"dimension_period_missing:{market}:{dimension}:{period}")
                    continue
                checked += 1
                actual = sum(value for value in values if value is not None)
                if not _close(actual, expected):
                    failures.append(
                        f"dimension_total_mismatch:{market}:{dimension}:{period}:actual={actual}:expected={expected}"
                    )
    return _gate(
        "general_dimension_parity",
        checked=checked,
        population=population,
        failures=failures,
        tolerance=f"absolute:{ABS_TOLERANCE}",
    )


def _segment_series(segments: Any, index: int) -> tuple[float | None, dict[str, float] | None]:
    if not isinstance(segments, list) or not segments:
        return None, None
    overall: float | None = None
    option_values: dict[str, float] = {}
    for segment in segments:
        if not isinstance(segment, Mapping):
            return None, None
        series = segment.get("value_series")
        if not isinstance(series, list) or index >= len(series):
            return None, None
        value = _point_value(series[index])
        label = str(segment.get("name") or segment.get("segment") or "").strip().casefold()
        if segment.get("is_overall") or label in {"전체", "overall", "total"}:
            overall = value
        elif value is None:
            return None, None
        else:
            normalized = _normalized_label(label)
            if not normalized or normalized in option_values:
                return None, None
            option_values[normalized] = value
    return overall, option_values if option_values else None


def _validate_blocks(evidence: Mapping[str, Any]) -> dict[str, Any]:
    market_totals, failures = _market_series(evidence.get("market_rows") or [])
    block_rows = list(evidence.get("block_rows") or [])
    population = sum(
        min(len(market_totals.get(str(row.get("market_id") or ""), {})), 60) * len(BLOCK_LEVEL_BY_DIMENSION)
        for row in block_rows
    )
    checked = 0
    for row in block_rows:
        market = str(row.get("market_id") or "")
        headline = market_totals.get(market)
        if headline is None:
            failures.append(f"block_headline_missing:{market}")
            continue
        payload = _json_object(row.get("analysis_levels_json"))
        periods = [str(item) for item in payload.get("periods_monthly") or []]
        expected_periods = list(headline)[-60:]
        if periods != expected_periods:
            failures.append(
                f"block_period_census_mismatch:{market}:actual={periods}:expected={expected_periods}"
            )
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        for period in expected_periods:
            if period not in periods:
                failures.extend(
                    f"block_period_missing:{market}:{dimension}:{period}"
                    for dimension in BLOCK_LEVEL_BY_DIMENSION
                )
                continue
            index = periods.index(period)
            expected = headline[period]
            for dimension, level in BLOCK_LEVEL_BY_DIMENSION.items():
                section = data.get(level) if isinstance(data, Mapping) else None
                if not isinstance(section, Mapping):
                    failures.append(f"block_level_missing:{market}:{level}")
                    continue
                by_channel = section.get("by_channel")
                segments = by_channel.get("전체") if isinstance(by_channel, Mapping) else section.get("segments")
                overall, options = _segment_series(segments, index)
                if overall is None or options is None:
                    failures.append(f"block_segments_incomplete:{market}:{level}:{period}")
                    continue
                checked += 1
                option_total = sum(options.values())
                if not _close(overall, expected):
                    failures.append(
                        f"block_overall_mismatch:{market}:{level}:{period}:actual={overall}:expected={expected}"
                    )
                if not _close(option_total, overall):
                    failures.append(
                        f"block_option_sum_mismatch:{market}:{level}:{period}:actual={option_total}:expected={overall}"
                    )
    return _gate(
        "analysis_level_block_parity",
        checked=checked,
        population=population,
        failures=failures,
        tolerance=f"absolute:{ABS_TOLERANCE}",
    )


def _dimension_option_map(rows: Sequence[Mapping[str, Any]], period: str) -> dict[str, float] | None:
    result: dict[str, float] = {}
    for row in rows:
        label = _normalized_label(row.get("dimension_value"))
        value = _history_value(row.get("raw_value_history"), period)
        if not label or value is None or label in result:
            return None
        result[label] = value
    return result or None


def _validate_sidecar_block_options(evidence: Mapping[str, Any]) -> dict[str, Any]:
    market_totals, failures = _market_series(evidence.get("market_rows") or [])
    dimension_rows: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in evidence.get("dimension_rows") or []:
        dimension_rows[(str(row.get("market_id") or ""), str(row.get("dimension_type") or ""))].append(row)
    block_rows = list(evidence.get("block_rows") or [])
    population = sum(
        min(len(market_totals.get(str(row.get("market_id") or ""), {})), 60) * len(SIDECAR_DIMENSION_TYPES)
        for row in block_rows
    )
    checked = 0
    for row in block_rows:
        market = str(row.get("market_id") or "")
        headline = market_totals.get(market)
        if not headline:
            failures.append(f"option_headline_missing:{market}")
            continue
        payload = _json_object(row.get("analysis_levels_json"))
        periods = [str(item) for item in payload.get("periods_monthly") or []]
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        for period in list(headline)[-60:]:
            if period not in periods:
                failures.extend(
                    f"option_period_missing:{market}:{dimension}:{period}"
                    for dimension in SIDECAR_DIMENSION_TYPES
                )
                continue
            index = periods.index(period)
            for dimension in SIDECAR_DIMENSION_TYPES:
                sidecar = _dimension_option_map(dimension_rows.get((market, dimension), []), period)
                level = BLOCK_LEVEL_BY_DIMENSION[dimension]
                section = data.get(level) if isinstance(data, Mapping) else None
                by_channel = section.get("by_channel") if isinstance(section, Mapping) else None
                segments = by_channel.get("전체") if isinstance(by_channel, Mapping) else None
                _overall, block = _segment_series(segments, index)
                if sidecar is None or block is None:
                    failures.append(f"option_payload_incomplete:{market}:{dimension}:{period}")
                    continue
                checked += 1
                if set(sidecar) != set(block):
                    failures.append(
                        f"option_labels_mismatch:{market}:{dimension}:{period}:"
                        f"sidecar={sorted(sidecar)}:block={sorted(block)}"
                    )
                    continue
                for label in sidecar:
                    if not _close(sidecar[label], block[label]):
                        failures.append(
                            f"option_value_mismatch:{market}:{dimension}:{period}:{label}:"
                            f"sidecar={sidecar[label]}:block={block[label]}"
                        )
    return _gate(
        "sidecar_block_option_parity",
        checked=checked,
        population=population,
        failures=failures,
        tolerance=f"absolute:{ABS_TOLERANCE}",
    )


def _specialty_total(value: Any, period: str) -> tuple[float | None, int]:
    payload = _json_object(value)
    if not payload:
        return None, 0
    values = [_history_value(history, period) for history in payload.values()]
    if not values or any(item is None for item in values):
        return None, len(values)
    return sum(item for item in values if item is not None), len(values)


def _validate_specialty(rows: Iterable[Mapping[str, Any]], gate_name: str) -> dict[str, Any]:
    row_list = list(rows)
    failures: list[str] = []
    checked = 0
    population = 0
    for row in row_list:
        identity = f"{row.get('market_id')}:{row.get('brand_name')}"
        metric_series = {
            period: amount
            for period, amount in _series_map(row.get("metric_history")).items()
            if amount is not None
        }
        if not metric_series:
            failures.append(f"specialty_metric_missing:{identity}")
            continue
        population += len(metric_series)
        for period, expected in metric_series.items():
            actual, option_count = _specialty_total(row.get("specialty_data"), period)
            if actual is None or option_count == 0:
                failures.append(f"specialty_coverage_missing:{identity}:{period}")
                continue
            checked += 1
            if not _close(actual, expected):
                failures.append(
                    f"specialty_total_mismatch:{identity}:{period}:actual={actual}:expected={expected}"
                )
    return _gate(
        gate_name,
        checked=checked,
        population=population,
        failures=failures,
        tolerance=f"absolute:{ABS_TOLERANCE}",
    )


def validate_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    gates = [
        _validate_freshness(evidence),
        _validate_dimensions(evidence),
        _validate_blocks(evidence),
        _validate_sidecar_block_options(evidence),
        _validate_specialty(evidence.get("general_specialty_rows") or [], "general_specialty_parity"),
        _validate_specialty(evidence.get("strategic_specialty_rows") or [], "strategic_specialty_parity"),
    ]
    return {
        "gates": gates,
        "exit_code": 1 if any(gate["exit_code"] for gate in gates) else 0,
    }


def _fetch_all(cursor: Any, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    cursor.execute(sql, params)
    return [dict(row) for row in cursor.fetchall()]


def _iso(value: Any) -> str | None:
    parsed = _utc_datetime(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def _collect_source_tables(
    cursor: Any,
    table_specs: Sequence[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    for logical_name, schema, table in table_specs:
        quoted = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
        cursor.execute(
            f"""
            SELECT COUNT(*) AS row_count,
                   MIN(computed_at) AS computed_at_min,
                   MAX(computed_at) AS computed_at_max
            FROM {quoted}
            WHERE source = %s AND measure = %s
            """,
            ("ubist", "sales"),
        )
        counts = dict(cursor.fetchone() or {})
        states.append(
            {
                "logical_name": logical_name,
                "table_schema": schema,
                "table_name": table,
                "row_count": int(counts.get("row_count") or 0),
                "computed_at_min": _iso(counts.get("computed_at_min")),
                "computed_at_max": _iso(counts.get("computed_at_max")),
            }
        )
    return states


def collect_runtime_evidence() -> dict[str, Any]:
    import pymysql

    host = os.environ.get("CHAT_QUERY_DB_HOST") or os.environ.get("CHAT_CACHE_DB_HOST", "llmops-mariadb-service.llmops.svc.cluster.local")
    port = int(os.environ.get("CHAT_QUERY_DB_PORT") or os.environ.get("CHAT_CACHE_DB_PORT", "3306"))
    database = os.environ.get("CHAT_QUERY_DB_NAME") or os.environ.get("CHAT_CACHE_DB_NAME", "jw_mart")
    general_dimension_database = os.environ.get("GENERAL_DIMENSION_DB_NAME", database)
    user = os.environ.get("CHAT_QUERY_DB_USER") or os.environ.get("CHAT_CACHE_DB_USER", "llmops")
    password = os.environ.get("CHAT_QUERY_DB_PASSWORD") or os.environ.get("CHAT_CACHE_DB_PASSWORD", "")
    connection = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        connect_timeout=5,
        read_timeout=60,
        write_timeout=60,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute("START TRANSACTION READ ONLY")
            try:
                cursor.execute("SELECT @@session.tx_read_only AS tx_read_only")
            except pymysql.MySQLError:
                cursor.execute("SELECT @@session.transaction_read_only AS tx_read_only")
            tx_read_only = int(cursor.fetchone()["tx_read_only"])
            cohorts = _fetch_all(
                cursor,
                """
                SELECT source_epoch, build_version, MIN(built_at) AS built_at_min,
                       MAX(built_at) AS built_at_max, COUNT(*) AS row_count
                FROM mart_analysis_level_block
                WHERE view = 'general' AND UPPER(source) = 'UBIST'
                  AND measure = 'sales' AND trim_mode = 'full'
                GROUP BY source_epoch, build_version
                ORDER BY MAX(built_at) DESC, source_epoch DESC
                LIMIT 1
                """,
            )
            cohort = cohorts[0] if cohorts else {}
            block_rows = _fetch_all(
                cursor,
                """
                SELECT market_id, source_epoch, build_version, analysis_levels_json
                FROM mart_analysis_level_block
                WHERE view = 'general' AND UPPER(source) = 'UBIST'
                  AND measure = 'sales' AND trim_mode = 'full'
                  AND source_epoch = %s AND build_version = %s
                ORDER BY market_id, profile_sig
                """,
                (cohort.get("source_epoch"), cohort.get("build_version")),
            ) if cohort else []
            market_rows = _fetch_all(
                cursor,
                """
                SELECT atc4_code AS market_id, market_size_series
                FROM mart_general_market_metric
                WHERE source = 'ubist' AND measure = 'sales'
                ORDER BY atc4_code
                """,
            )
            dimension_rows = _fetch_all(
                cursor,
                f"""
                SELECT atc4_code AS market_id, dimension_type, dimension_value, raw_value_history
                FROM {_quote_identifier(general_dimension_database)}.mart_general_filter_dimension_metric
                WHERE source = 'ubist' AND measure = 'sales'
                  AND dimension_type IN (%s, %s, %s, %s, %s)
                ORDER BY atc4_code, dimension_type, dimension_value_norm
                """,
                SIDECAR_DIMENSION_TYPES,
            )
            general_specialty_rows = _fetch_all(
                cursor,
                """
                SELECT atc4_code AS market_id, brand_name, metric_history, specialty_data
                FROM mart_general_brand_metric
                WHERE source = 'ubist' AND measure = 'sales'
                ORDER BY atc4_code, brand_name, brand_key
                """,
            )
            strategic_specialty_rows = _fetch_all(
                cursor,
                """
                SELECT ml_id AS market_id, brand_name, metric_history, specialty_data
                FROM mart_strategic_ml_brand_metric
                WHERE source = 'ubist' AND measure = 'sales'
                ORDER BY ml_id, brand_name, brand_key
                """,
            )
            source_tables = _collect_source_tables(
                cursor,
                (
                    ("general_brand", database, "mart_general_brand_metric"),
                    ("general_market", database, "mart_general_market_metric"),
                    ("general_dimension", general_dimension_database, "mart_general_filter_dimension_metric"),
                    ("strategic_brand", database, "mart_strategic_ml_brand_metric"),
                    ("strategic_market", database, "mart_strategic_ml_market_metric"),
                ),
            )
        connection.rollback()
    finally:
        connection.close()
    return {
        "tx_read_only": tx_read_only,
        "cohort": {
            "source_epoch": cohort.get("source_epoch"),
            "build_version": cohort.get("build_version"),
            "built_at_min": _iso(cohort.get("built_at_min")),
            "built_at_max": _iso(cohort.get("built_at_max")),
            "row_count": int(cohort.get("row_count") or 0),
        },
        "source_tables": source_tables,
        "market_rows": market_rows,
        "dimension_rows": dimension_rows,
        "block_rows": block_rows,
        "general_specialty_rows": general_specialty_rows,
        "strategic_specialty_rows": strategic_specialty_rows,
    }


def _acceptance_line(gate: Mapping[str, Any]) -> str:
    return " ".join(
        f"{key}={gate[key]}"
        for key in (
            "gate",
            "classification",
            "checked",
            "population",
            "missing",
            "tolerance",
            "failure_count",
            "exit_code",
            "environment",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, help="Validate captured JSON instead of querying the runtime mart")
    parser.add_argument("--output", type=Path, help="Write the redacted validation report")
    args = parser.parse_args()
    if args.evidence is None and os.environ.get("MART_RELOAD_COMPLETE") != "1":
        print("gate=mart_reload_authorization classification=census checked=0 population=1 missing=fail tolerance=exact failure_count=1 exit_code=1 environment=runtime_mart_read_only")
        return 1
    evidence = json.loads(args.evidence.read_text(encoding="utf-8")) if args.evidence else collect_runtime_evidence()
    report = validate_evidence(evidence)
    for gate in report["gates"]:
        print(_acceptance_line(gate))
        for failure in gate["failures"]:
            print(f"failure gate={gate['gate']} reason={failure}")
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
