"""Resolve UBIST facility-specialty channels for cause cache payloads."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from pipeline.scripts.utils.ubist_channel_mapping import parse_channel_code, raw_pair_to_channel_code
from pipeline.scripts.utils.ubist_target_channel_mapping import (
    TargetUbistChannel,
    parse_target_channel_code,
    raw_pair_to_target_channel_code,
    translate_target_channel_to_raw_labels,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
UBIST_PARQUET_GLOB = PROJECT_ROOT / "output" / "ubist" / "year=*" / "month=*" / "data.parquet"
SCREEN_FACILITY_CHANNELS = ["전체", "상급종병", "종병", "(상급종병 + 종병)", "병원", "의원", "보건소", "기타"]
UBIST_CHANNEL_BY_DISPLAY_COLUMN = "ubist_channel_by_display"
UBIST_CHANNEL_BY_CODE_COLUMN = "ubist_channel_by_code"
CHANNEL_SPECIALTY_MATRIX_COLUMN = "channel_specialty_matrix"
_STRATEGIC_CHANNEL_ROWS: ContextVar[tuple[dict[str, Any], ...] | None] = ContextVar(
    "strategic_channel_rows",
    default=None,
)


def _clean_target(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    return text if text and text.lower() != "nan" else None


def _targets_from_market(market: dict[str, Any] | None) -> list[str]:
    market = market or {}
    return [
        target
        for target in (_clean_target(market.get(f"target_ubist_{index}")) for index in range(1, 5))
        if target
    ]


def _value_column(measure: str) -> str:
    return "rx_qty" if str(measure).lower() in {"volume", "qty", "count", "unit"} else "rx_amt"


def _brand_names(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    names = {
        str(row.get("brand_name") or row.get("brand_key") or "").strip()
        for row in rows
        if row.get("brand_name") or row.get("brand_key")
    }
    return tuple(sorted(name for name in names if name))


def _load_market_raw_totals(brand_names: tuple[str, ...], measure: str) -> tuple[dict[str, dict[str, dict[str, float]]], dict[str, float]]:
    """Return raw UBIST value series by brand and resolved channel display.

    Strategic mart rows now persist the parquet-era resolver contract in
    ``ubist_channel_by_display`` and ``ubist_channel_by_code``. When callers
    provide those DB rows through ``strategic_channel_totals_context``, this
    adapter returns the exact tuple that the old parquet reader produced while
    keeping ``resolve_market_channels`` selection logic unchanged.

    Shape:
      by_brand[brand][display][period] = value
      totals_by_code[code] = total value across the full raw window
    """
    if not brand_names:
        return {}, {}

    strategic_rows = _STRATEGIC_CHANNEL_ROWS.get()
    if strategic_rows is not None:
        return _load_market_raw_totals_from_strategic_rows(brand_names, strategic_rows)

    return _load_market_raw_totals_from_parquet(brand_names, measure)


@lru_cache(maxsize=64)
def _load_market_raw_totals_from_parquet(brand_names: tuple[str, ...], measure: str) -> tuple[dict[str, dict[str, dict[str, float]]], dict[str, float]]:
    """Read the legacy local parquet source when no strategic DB row context exists."""
    try:
        import duckdb
    except ImportError:
        return {}, {}

    parquet_glob = UBIST_PARQUET_GLOB.as_posix()
    if not list((PROJECT_ROOT / "output" / "ubist").glob("year=*/month=*/data.parquet")):
        return {}, {}

    value_col = _value_column(measure)
    placeholders = ",".join("?" for _ in brand_names)
    sql = f"""
        SELECT
          브랜드 AS brand,
          period_yyyymm AS period,
          종별 AS facility_raw,
          진료과 AS specialty_raw,
          SUM(TRY_CAST({value_col} AS DOUBLE)) AS value
        FROM read_parquet('{parquet_glob}', hive_partitioning=true)
        WHERE 브랜드 IN ({placeholders})
          AND TRY_CAST({value_col} AS DOUBLE) > 0
        GROUP BY 브랜드, period_yyyymm, 종별, 진료과
    """
    rows = duckdb.connect(":memory:").execute(sql, list(brand_names)).fetchall()

    by_brand: dict[str, dict[str, dict[str, float]]] = {}
    totals_by_code: dict[str, float] = {}
    display_by_code: dict[str, str] = {}
    for brand, period, facility_raw, specialty_raw, value in rows:
        code = raw_pair_to_channel_code(facility_raw, specialty_raw)
        if not code:
            continue
        parsed = parse_channel_code(code)
        if parsed is None:
            continue
        display = parsed.display_name
        numeric = float(value or 0.0)
        display_by_code[code] = display
        totals_by_code[code] = totals_by_code.get(code, 0.0) + numeric
        brand_bucket = by_brand.setdefault(str(brand), {}).setdefault(display, {})
        period_text = str(period)
        brand_bucket[period_text] = brand_bucket.get(period_text, 0.0) + numeric

    # The caller only needs totals by code for ranking fallback, but keeping
    # display generation here validates that every code can be parsed.
    return by_brand, totals_by_code


@contextmanager
def strategic_channel_totals_context(rows: list[dict[str, Any]]) -> Iterator[None]:
    """Provide strategic DB rows that contain the UBIST channel contract columns.

    `_load_market_raw_totals` historically had only `(brand_names, measure)` as
    inputs because parquet was a global local source. Strategic mart data is
    market-scoped, so callers pass the already-fetched DB sibling rows through
    this context. The resolver contract and channel selection remain unchanged.
    """
    token = _STRATEGIC_CHANNEL_ROWS.set(tuple(dict(row) for row in rows))
    try:
        yield
    finally:
        _STRATEGIC_CHANNEL_ROWS.reset(token)


def _load_market_raw_totals_from_strategic_rows(
    brand_names: tuple[str, ...],
    rows: tuple[dict[str, Any], ...],
) -> tuple[dict[str, dict[str, dict[str, float]]], dict[str, float]]:
    requested = set(brand_names)
    by_brand: dict[str, dict[str, dict[str, float]]] = {}
    totals_by_code: dict[str, float] = {}
    for row in rows:
        brand = str(row.get("brand_name") or row.get("brand_key") or "").strip()
        if not brand or brand not in requested:
            continue
        by_display = _decode_object(row.get(UBIST_CHANNEL_BY_DISPLAY_COLUMN))
        by_code = _decode_object(row.get(UBIST_CHANNEL_BY_CODE_COLUMN))
        target_display = _normalize_target_channel_series(by_code)
        if target_display:
            by_brand[brand] = target_display
        elif by_display:
            by_brand[brand] = _normalize_channel_series(by_display)
        for code, series in by_code.items():
            if not isinstance(series, dict):
                continue
            totals_by_code[str(code)] = totals_by_code.get(str(code), 0.0) + sum(
                _history_value(value) for value in series.values()
            )
    return by_brand, totals_by_code


def _decode_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _normalize_channel_series(payload: dict[str, Any]) -> dict[str, dict[str, float]]:
    normalized: dict[str, dict[str, float]] = {}
    for channel, series in payload.items():
        if not isinstance(series, dict):
            continue
        normalized[str(channel)] = {
            str(period): _history_value(value)
            for period, value in series.items()
            if _history_value(value) > 0.0
        }
    return normalized


def _normalize_target_channel_series(payload: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Expose strategic channel series using MI Master target labels.

    The mart contract stores reusable UBIST channel totals with the general
    parser keys.  Strategic views need the target-channel vocabulary only at
    read time, so this adapter keeps the global GH/general parser unchanged.
    """

    normalized: dict[str, dict[str, float]] = {}
    for code, series in payload.items():
        if not isinstance(series, dict):
            continue
        parsed = parse_target_channel_code(str(code))
        if parsed is None:
            continue
        bucket = normalized.setdefault(parsed.display_name, {})
        for period, value in series.items():
            numeric = _history_value(value)
            if numeric <= 0.0:
                continue
            period_text = str(period)
            bucket[period_text] = bucket.get(period_text, 0.0) + numeric
    return normalized


def _raw_matrices_available(rows: list[dict[str, Any]]) -> bool:
    return any(bool(_decode_object(row.get(CHANNEL_SPECIALTY_MATRIX_COLUMN))) for row in rows)


def _parse_target_label(label: str | None) -> tuple[str, str]:
    if not label:
        return "", ""
    text = str(label)
    if " " not in text:
        return text, ""
    channel, specialty = text.split(" ", 1)
    return channel, specialty


def _iter_raw_matrix(row: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    matrix = _decode_object(row.get(CHANNEL_SPECIALTY_MATRIX_COLUMN))
    for facility, specialties in matrix.items():
        if not isinstance(specialties, dict):
            continue
        for specialty, series in specialties.items():
            if isinstance(series, dict):
                yield str(facility), str(specialty), series


def _raw_periods(rows: list[dict[str, Any]]) -> list[str]:
    periods: set[str] = set()
    for row in rows:
        for _facility, _specialty, series in _iter_raw_matrix(row):
            periods.update(str(period) for period in series)
    return sorted(periods)


def _raw_combo_total(rows: list[dict[str, Any]], facility: str, specialty: str) -> float:
    total = 0.0
    for row in rows:
        for matrix_facility, matrix_specialty, series in _iter_raw_matrix(row):
            if matrix_facility != facility or matrix_specialty != specialty:
                continue
            total += sum(_history_value(value) for value in series.values())
    return total


def _raw_combo_values_for_period(rows: list[dict[str, Any]], period: str) -> dict[tuple[str, str], float]:
    values: dict[tuple[str, str], float] = {}
    for row in rows:
        for facility, specialty, series in _iter_raw_matrix(row):
            value = _history_value(series.get(period))
            if value <= 0.0:
                continue
            key = (facility, specialty)
            values[key] = values.get(key, 0.0) + value
    return values


def _largest_raw_label(raw_labels: list[str], rows: list[dict[str, Any]]) -> str | None:
    if not raw_labels:
        return None
    return max(raw_labels, key=lambda label: _raw_combo_total(rows, *_parse_target_label(label)))


def _target_series_for_row(row: dict[str, Any], channel: TargetUbistChannel) -> dict[str, float]:
    series_by_period: dict[str, float] = {}
    facilities = set(channel.facility_raw_values)
    specialties = set(channel.specialty_raw_values)
    for facility, specialty, series in _iter_raw_matrix(row):
        if facility not in facilities or specialty not in specialties:
            continue
        for period, value in series.items():
            numeric = _history_value(value)
            if numeric <= 0.0:
                continue
            period_text = str(period)
            series_by_period[period_text] = series_by_period.get(period_text, 0.0) + numeric
    return dict(sorted(series_by_period.items()))


def _resolve_channels_from_raw_matrix(
    *,
    rows: list[dict[str, Any]],
    market: dict[str, Any] | None,
    max_channels: int,
) -> tuple[list[TargetUbistChannel], set[str]]:
    """Resolve target/fill channels with the ETL target-competition algorithm.

    The dynamic runtime must mirror ``market_target_competition.py`` instead of
    ranking precomputed mart channel totals: catalog slots keep their rank,
    catalog codes are expanded to raw candidates for exclusion, and remaining
    slots are filled from the latest-period raw facility-specialty matrix.
    """

    channels: list[TargetUbistChannel] = []
    used_codes: set[str] = set()
    target_codes: set[str] = set()
    used_raw_labels: set[str] = set()

    for target in _targets_from_market(market):
        parsed = parse_target_channel_code(target)
        if parsed is None or parsed.code in used_codes:
            continue
        raw_candidates = translate_target_channel_to_raw_labels(target)
        chosen_raw = _largest_raw_label(raw_candidates, rows)
        if raw_candidates:
            used_raw_labels.update(raw_candidates)
        if chosen_raw is None:
            continue
        channels.append(parsed)
        used_codes.add(parsed.code)
        target_codes.add(parsed.code)

    periods = _raw_periods(rows)
    latest_period = periods[-1] if periods else None
    if latest_period and len(channels) < max_channels:
        combo_values = _raw_combo_values_for_period(rows, latest_period)
        for (facility, specialty), _value in sorted(combo_values.items(), key=lambda item: item[1], reverse=True):
            if len(channels) >= max_channels:
                break
            raw_label = f"{facility} {specialty}"
            if raw_label in used_raw_labels:
                continue
            code = raw_pair_to_target_channel_code(facility, specialty)
            if not code:
                continue
            parsed = parse_target_channel_code(code)
            if parsed is None or parsed.code in used_codes:
                continue
            channels.append(parsed)
            used_codes.add(parsed.code)
            used_raw_labels.update(translate_target_channel_to_raw_labels(parsed.code))

    return channels, target_codes


def _attach_raw_matrix_channel_series(rows: list[dict[str, Any]], channels: list[TargetUbistChannel]) -> dict[str, dict[str, dict[str, float]]]:
    series_by_brand: dict[str, dict[str, dict[str, float]]] = {}
    for row in rows:
        brand = str(row.get("brand_name") or row.get("brand_key") or "").strip()
        if not brand:
            continue
        channel_series: dict[str, dict[str, float]] = {}
        for channel in channels:
            series = _target_series_for_row(row, channel)
            if series:
                channel_series[channel.display_name] = series
        series_by_brand[brand] = channel_series
        row["__ubist_dual_channel_data"] = channel_series
        row["__ubist_specialty_channel_data"] = channel_series
    return series_by_brand


def _history_value(value: Any) -> float:
    raw = value.get("raw_value", value.get("value", 0.0)) if isinstance(value, dict) else value
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def resolve_market_channels(
    *,
    rows: list[dict[str, Any]],
    market: dict[str, Any] | None,
    measure: str,
    max_channels: int = 4,
) -> dict[str, Any]:
    """Resolve UBIST target channels and attach raw series to mart rows."""
    if _raw_matrices_available(rows):
        channels, target_codes = _resolve_channels_from_raw_matrix(
            rows=rows,
            market=market,
            max_channels=max_channels,
        )
        series_by_brand = _attach_raw_matrix_channel_series(rows, channels)
        display_names = [channel.display_name for channel in channels]
        return {
            "channels": list(SCREEN_FACILITY_CHANNELS),
            "specialty_channels": ["전체", *display_names] if display_names else ["전체"],
            "target_channels": [channel.as_dict() for channel in channels],
            "specialty_target_channels": [channel.as_dict() for channel in channels],
            "fallback_codes": [channel.code for channel in channels if channel.code not in target_codes],
            "series_brand_count": len(series_by_brand),
            "raw_brand_count": len(_brand_names(rows)),
        }

    brand_names = _brand_names(rows)
    series_by_brand, totals_by_code = _load_market_raw_totals(brand_names, measure)

    channels = []
    used_codes: set[str] = set()
    target_codes: set[str] = set()
    for target in _targets_from_market(market):
        parsed = parse_target_channel_code(target)
        if parsed is None or parsed.code in used_codes:
            continue
        channels.append(parsed)
        used_codes.add(parsed.code)
        target_codes.add(parsed.code)

    if len(channels) < max_channels:
        for code, _ in sorted(totals_by_code.items(), key=lambda item: item[1], reverse=True):
            if len(channels) >= max_channels:
                break
            parsed = parse_target_channel_code(code)
            if parsed is None or parsed.code in used_codes:
                continue
            channels.append(parsed)
            used_codes.add(parsed.code)

    display_names = [channel.display_name for channel in channels]
    for row in rows:
        brand = str(row.get("brand_name") or row.get("brand_key") or "").strip()
        row["__ubist_dual_channel_data"] = series_by_brand.get(brand, {})
        row["__ubist_specialty_channel_data"] = series_by_brand.get(brand, {})

    return {
        "channels": list(SCREEN_FACILITY_CHANNELS),
        "specialty_channels": ["전체", *display_names] if display_names else ["전체"],
        "target_channels": [channel.as_dict() for channel in channels],
        "specialty_target_channels": [channel.as_dict() for channel in channels],
        "fallback_codes": [channel.code for channel in channels if channel.code not in target_codes],
        "series_brand_count": len(series_by_brand),
        "raw_brand_count": len(brand_names),
    }
