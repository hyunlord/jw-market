"""Resolve UBIST facility-specialty channels for cause cache payloads."""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from pipeline.scripts.utils.ubist_channel_mapping import parse_channel_code, raw_pair_to_channel_code


PROJECT_ROOT = Path(__file__).resolve().parents[3]
UBIST_PARQUET_GLOB = PROJECT_ROOT / "output" / "ubist" / "year=*" / "month=*" / "data.parquet"
SCREEN_FACILITY_CHANNELS = ["전체", "상급종병", "종병", "병원", "의원", "보건소", "기타"]


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


@lru_cache(maxsize=64)
def _load_market_raw_totals(brand_names: tuple[str, ...], measure: str) -> tuple[dict[str, dict[str, dict[str, float]]], dict[str, float]]:
    """Return raw UBIST value series by brand and resolved channel display.

    Shape:
      by_brand[brand][display][period] = value
      totals_by_code[code] = total value across the full raw window
    """
    if not brand_names:
        return {}, {}

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


def resolve_market_channels(
    *,
    rows: list[dict[str, Any]],
    market: dict[str, Any] | None,
    measure: str,
    max_channels: int = 4,
) -> dict[str, Any]:
    """Resolve UBIST target channels and attach raw series to mart rows."""
    brand_names = _brand_names(rows)
    series_by_brand, totals_by_code = _load_market_raw_totals(brand_names, measure)

    channels = []
    used_codes: set[str] = set()
    for target in _targets_from_market(market):
        parsed = parse_channel_code(target)
        if parsed is None or parsed.code in used_codes:
            continue
        channels.append(parsed)
        used_codes.add(parsed.code)

    if len(channels) < max_channels:
        for code, _ in sorted(totals_by_code.items(), key=lambda item: item[1], reverse=True):
            if len(channels) >= max_channels:
                break
            if code in used_codes:
                continue
            parsed = parse_channel_code(code)
            if parsed is None:
                continue
            channels.append(parsed)
            used_codes.add(code)

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
        "fallback_codes": [channel.code for channel in channels if channel.code not in set(_targets_from_market(market))],
        "series_brand_count": len(series_by_brand),
        "raw_brand_count": len(brand_names),
    }
