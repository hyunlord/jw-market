#!/usr/bin/env python3
"""Build spec-aligned cache_cause from Phase 1 strategic marts."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cache_build_common import (
    MEASURES_BY_SOURCE,
    api_source,
    calculate_ei_with_fallback,
    decode_json,
    dump_payload,
    fetch_all,
    load_catalog,
    metric_recent,
    ml_to_strategy,
    mariadb_connect,
    parser,
    period_key,
    optional_float,
    safe_float,
    series_cagr,
    series_latest_number,
    source_list,
)
from pipeline.scripts.api.metadata.ml_market_meta import BRAND_METADATA
from pipeline.scripts.etl.ubist_channel_resolver import resolve_market_channels

period_key = lru_cache(maxsize=None)(period_key)


CHANNELS_5 = ["전체", "상급종병", "종병", "병원", "의원/보건소"]
IQVIA_CHANNELS = ["전체", "KHPA", "KCPA", "KPA"]
CAUSE_LEVELS_V091 = ["Class", "Molecule", "Brand", "제형/투여경로", "용량", "비/급여", "Ox/Gx"]
CAUSE_LEVELS_ML011 = ["Class 1", "Class 2", "Molecule", "Brand", "제형/투여경로", "용량", "비/급여", "Ox/Gx"]
FISH_OIL_LEVEL = "Fish Oil"
LEVEL_FIELD_BY_LABEL = {
    "Class": "class",
    "Class 1": "class_1",
    "Class 2": "class_2",
    "Molecule": "molecule",
    "제형/투여경로": "dosage_form",
    "용량": "strength_pack",
    "비/급여": "nhi_type",
    "Ox/Gx": "ox_gx",
    FISH_OIL_LEVEL: "fish_oil",
    "fish_oil": "fish_oil",
}
ANALYSIS_LEVELS_CACHE: dict[tuple[str | None, str, str], dict[str, Any]] = {}
LEVEL_ROW_GROUPS_CACHE: dict[tuple[str | None, str, str], dict[str, dict[str, list[dict[str, Any]]]]] = {}
EI_META_CACHE: dict[tuple[Any, Any], dict[str, Any]] = {}
TARGET_RANK_STATS_CACHE: dict[Any, dict[int, dict[str, dict[str, Any]]]] = {}
BRAND_METADATA_BY_NAME = {item.brand: item for item in BRAND_METADATA}


def _period_year(period: str) -> int | None:
    try:
        return int(str(period)[:4])
    except (TypeError, ValueError):
        return None


def _row_value(row: dict[str, Any]) -> float:
    return safe_float(row.get("raw_value") or row.get("value") or row.get("sales")) or 0.0


def _row_share(row: dict[str, Any]) -> float:
    return safe_float(row.get("ms") or row.get("ms_pct") or row.get("share_pct")) or 0.0


def _row_brand(row: dict[str, Any]) -> str | None:
    value = row.get("brand_name") or row.get("brand") or row.get("brand_key") or row.get("name")
    return str(value) if value not in (None, "") else None


def _row_company(row: dict[str, Any]) -> str | None:
    if "__company" in row:
        return row["__company"]
    for key in ("company", "company_name", "manufacturer", "raw_company"):
        value = row.get(key)
        if value not in (None, ""):
            row["__company"] = str(value)
            return str(value)
    by_dimension = row.get("__by_dimension")
    if by_dimension is None:
        by_dimension = decode_json(row.get("by_dimension"))
        row["__by_dimension"] = by_dimension
    if isinstance(by_dimension, dict):
        for key in ("company", "manufacturer", "raw_company"):
            value = by_dimension.get(key)
            if value not in (None, ""):
                row["__company"] = str(value)
                return str(value)
    row["__company"] = None
    return None


def _company_name(row: dict[str, Any]) -> str:
    return _row_company(row) or "Unknown"


def _metric_history(row: dict[str, Any]) -> dict[str, Any]:
    history = row.get("__metric_history")
    if history is None:
        history = decode_json(row.get("metric_history"))
        row["__metric_history"] = history
    return history if isinstance(history, dict) else {}


def _latest_history_item(row: dict[str, Any]) -> dict[str, Any]:
    cached = row.get("__latest_history_item")
    if cached is not None:
        return cached
    history = _metric_history(row)
    if not history:
        row["__latest_history_item"] = {}
        return {}
    latest_period = sorted(history.keys(), key=period_key)[-1]
    item = history.get(latest_period)
    result = item if isinstance(item, dict) else {"raw_value": item}
    row["__latest_history_item"] = result
    return result


def _latest_extended_item(row: dict[str, Any]) -> dict[str, Any]:
    cached = row.get("__latest_extended_item")
    if cached is not None:
        return cached
    history = row.get("__extended_metric_history")
    if history is None:
        history = decode_json(row.get("extended_metric_history"))
        row["__extended_metric_history"] = history
    if not isinstance(history, dict) or not history:
        row["__latest_extended_item"] = {}
        return {}
    latest_period = sorted(history.keys(), key=period_key)[-1]
    item = history.get(latest_period)
    result = item if isinstance(item, dict) else {}
    row["__latest_extended_item"] = result
    return result


def _normalize_rank_row(row: dict[str, Any], *, label_key: str, target_name: str | None) -> dict[str, Any]:
    name = row.get(label_key) or row.get("brand") or row.get("brand_key") or row.get("company") or row.get("name")
    is_target = bool(target_name and name == target_name)
    return {
        label_key: name,
        "brand": name if label_key == "brand" else row.get("brand"),
        "company": row.get("company") or row.get("company_name"),
        "is_target": is_target,
        "is_jw": bool(row.get("is_jw")) or is_target,
        "is_others": False,
        "value": _row_value(row),
        "rank": row.get("rank"),
        "ms_pct": _row_share(row),
    }


def _stacked_ranking(
    period_map: dict[str, Any],
    *,
    label_key: str,
    target_name: str | None,
    catalog_members: list[dict[str, Any]] | None = None,
    target_overrides: dict[int, dict[str, Any]] | None = None,
    top_n: int = 5,
    full_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    by_year, period_count_by_year = _annual_rank_rows(
        period_map,
        label_key=label_key,
        target_name=target_name,
        full_rows=full_rows,
    )

    years = sorted(by_year.keys())[-5:]
    yearly = []
    normalized_by_year: dict[int, list[dict[str, Any]]] = {}

    latest_rows = [row for row in by_year.get(years[-1], []) if row_identity(row, label_key)] if years else []
    latest_ranked = sorted(latest_rows, key=lambda item: safe_float(item.get("value")) or 0.0, reverse=True)
    target = next((row for row in latest_ranked if target_name and row_identity(row, label_key) == target_name), None)
    target_id = row_identity(target, label_key)
    competitors = [row for row in latest_ranked if row_identity(row, label_key) and row_identity(row, label_key) != target_id]
    fixed = ([target] if target else []) + competitors[:top_n]
    fixed_ids = [row_identity(row, label_key) for row in fixed if row_identity(row, label_key)]

    for year in years:
        normalized = deepcopy(by_year[year])
        override = (target_overrides or {}).get(year)
        if override:
            target_index = next(
                (index for index, row in enumerate(normalized) if row_identity(row, label_key) == target_name),
                None,
            )
            if target_index is None:
                normalized.append(override)
            else:
                normalized[target_index] = {**normalized[target_index], **override}
        existing = {row.get(label_key) for row in normalized}
        if catalog_members:
            for member in catalog_members:
                name = member.get("name")
                if name and name not in existing:
                    normalized.append(
                        {
                            label_key: name,
                            "brand": name if label_key == "brand" else None,
                            "company": member.get("company"),
                            "is_target": bool(target_name and name == target_name),
                            "is_jw": bool(member.get("is_jw")),
                            "is_others": False,
                            "value": 0.0,
                            "rank": None,
                            "ms_pct": 0.0,
                        }
                    )
                    existing.add(name)

        normalized = _rank_normalized_rows(normalized, label_key=label_key)
        normalized_by_year[year] = deepcopy(normalized)

        row_by_id = {row_identity(row, label_key): row for row in normalized if row_identity(row, label_key)}
        selected = []
        for item_id in fixed_ids:
            row = row_by_id.get(item_id)
            if row is None:
                row = _zero_rank_row(item_id, label_key=label_key, target_name=target_name)
            selected.append(row)
        selected_ids = {row_identity(row, label_key) for row in selected}
        others = [row for row in normalized if row_identity(row, label_key) not in selected_ids]
        displayed_ms = sum(float(row.get("ms_pct") or 0.0) for row in selected)
        selected.append(
            {
                label_key: "기타",
                "brand": "기타" if label_key == "brand" else None,
                "company": "기타" if label_key == "company" else None,
                "is_target": False,
                "is_jw": False,
                "is_others": True,
                "value": sum(safe_float(row.get("value")) or 0.0 for row in others),
                "rank": None,
                "ms_pct": round(max(0.0, 100.0 - displayed_ms), 4),
            }
        )
        yearly.append({"year": year, "rankings": selected})

    trend_key = "brands" if label_key == "brand" else "companies"
    top_brands = [row_identity(row, label_key) for row in (fixed + [_zero_rank_row("기타", label_key=label_key, target_name=None)])]
    top_brands = [str(name) for name in top_brands if name]
    series = {
        name: [
            safe_float(next((row.get("value") for row in item["rankings"] if row_identity(row, label_key) == name), 0.0)) or 0.0
            for item in yearly
        ]
        for name in top_brands
    }
    rankings_by_year = {
        str(year): [
            {
                "rank": row.get("rank"),
                label_key: row.get(label_key),
                "brand": row.get("brand"),
                "company": row.get("company"),
                "value": safe_float(row.get("value")) or 0.0,
                "ms_pct": safe_float(row.get("ms_pct")) or 0.0,
                "is_target": bool(row.get("is_target")),
                "is_jw": bool(row.get("is_jw")),
            }
            for row in normalized_by_year.get(year, [])
            if row_identity(row, label_key)
        ]
        for year in years
    }
    return {
        "years": years,
        "yearly": yearly,
        trend_key: _latest_top_trends(
            years=years,
            normalized_by_year=normalized_by_year,
            label_key=label_key,
            target_name=target_name,
            top_n=top_n,
        ),
        "top_brands": top_brands,
        "series": series,
        "rankings_by_year": rankings_by_year,
        "period_count_by_year": {str(year): period_count_by_year.get(year, 0) for year in years},
    }


def _annual_rank_rows(
    period_map: dict[str, Any],
    *,
    label_key: str,
    target_name: str | None,
    full_rows: list[dict[str, Any]] | None = None,
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, int]]:
    if full_rows:
        return _annual_rank_rows_from_full_rows(full_rows, label_key=label_key, target_name=target_name)
    grouped: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    period_count_by_year: dict[int, int] = defaultdict(int)
    for period, rows in sorted((period_map or {}).items(), key=lambda pair: period_key(str(pair[0]))):
        year = _period_year(str(period))
        if year is None or not isinstance(rows, list):
            continue
        period_count_by_year[year] += 1
        for row in rows:
            normalized = _normalize_rank_row(row, label_key=label_key, target_name=target_name)
            name = row_identity(normalized, label_key)
            if not name:
                continue
            bucket = grouped[year].setdefault(
                name,
                {**normalized, "value": 0.0, "ms_pct": 0.0, "rank": None},
            )
            bucket["value"] = (safe_float(bucket.get("value")) or 0.0) + (safe_float(normalized.get("value")) or 0.0)
            bucket["is_target"] = bool(bucket.get("is_target") or normalized.get("is_target"))
            bucket["is_jw"] = bool(bucket.get("is_jw") or normalized.get("is_jw"))
    return {year: _rank_normalized_rows(list(rows.values()), label_key=label_key) for year, rows in grouped.items()}, dict(period_count_by_year)


def _annual_rank_rows_from_full_rows(
    rows: list[dict[str, Any]], *, label_key: str, target_name: str | None
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, int]]:
    periods_by_year: dict[int, set[str]] = defaultdict(set)
    grouped: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        history = _metric_history(row)
        if not history:
            continue
        if label_key == "company":
            name = _row_company(row)
        else:
            name = _row_brand(row)
        if not name:
            continue
        for period, item in history.items():
            year = _period_year(str(period))
            if year is None:
                continue
            periods_by_year[year].add(str(period))
            bucket = grouped[year].setdefault(
                name,
                {
                    label_key: name,
                    "brand": name if label_key == "brand" else None,
                    "company": _row_company(row) if label_key == "brand" else name,
                    "is_target": bool(target_name and name == target_name),
                    "is_jw": bool(row.get("is_jw")) or bool(target_name and name == target_name),
                    "is_others": False,
                    "value": 0.0,
                    "rank": None,
                    "ms_pct": 0.0,
                },
            )
            bucket["value"] += _value_from_period_item(item)
            bucket["is_jw"] = bool(bucket.get("is_jw") or row.get("is_jw"))
    return {year: _rank_normalized_rows(list(items.values()), label_key=label_key) for year, items in grouped.items()}, {
        year: len(periods) for year, periods in periods_by_year.items()
    }


def _rank_normalized_rows(rows: list[dict[str, Any]], *, label_key: str) -> list[dict[str, Any]]:
    ranked = sorted(rows, key=lambda item: safe_float(item.get("value")) or 0.0, reverse=True)
    total = sum(safe_float(row.get("value")) or 0.0 for row in ranked)
    for index, row in enumerate(ranked, start=1):
        value = safe_float(row.get("value")) or 0.0
        row["rank"] = index if value > 0 else None
        row["ms_pct"] = round(value / total * 100, 4) if total > 0 else 0.0
        row.setdefault("is_others", False)
        row.setdefault("brand", row.get(label_key) if label_key == "brand" else None)
        row.setdefault("company", row.get(label_key) if label_key == "company" else row.get("company"))
    return ranked


def _zero_rank_row(name: str, *, label_key: str, target_name: str | None) -> dict[str, Any]:
    return {
        label_key: name,
        "brand": name if label_key == "brand" else None,
        "company": name if label_key == "company" else None,
        "is_target": bool(target_name and name == target_name),
        "is_jw": bool(target_name and name == target_name),
        "is_others": name == "기타",
        "value": 0.0,
        "rank": None,
        "ms_pct": 0.0,
    }


def _latest_top_trends(
    *,
    years: list[int],
    normalized_by_year: dict[int, list[dict[str, Any]]],
    label_key: str,
    target_name: str | None,
    top_n: int,
) -> list[dict[str, Any]]:
    if not years:
        return []
    latest_year = years[-1]

    def identity(row: dict[str, Any]) -> str | None:
        return row_identity(row, label_key)

    def ranked_rows(year: int) -> list[dict[str, Any]]:
        rows = [
            row
            for row in normalized_by_year.get(year, [])
            if identity(row) and not row.get("is_others") and (safe_float(row.get("value")) or 0.0) > 0
        ]
        ranked = sorted(rows, key=lambda item: safe_float(item.get("value")) or 0.0, reverse=True)
        for index, row in enumerate(ranked, start=1):
            row.setdefault("rank", index)
        return ranked

    latest_ranked = ranked_rows(latest_year)
    target = next((row for row in latest_ranked if target_name and identity(row) == target_name), None)
    target_id = identity(target)
    competitors = [row for row in latest_ranked if identity(row) and identity(row) != target_id]
    latest_top = ([target] if target else []) + competitors[:top_n]
    selected_ids = {identity(row) for row in latest_top if identity(row)}
    others_ids = [identity(row) for row in competitors[top_n:] if identity(row)]
    trends = []
    for latest in latest_top:
        item_id = identity(latest)
        if not item_id:
            continue
        yearly_values = []
        for year in years:
            rows = ranked_rows(year)
            row = next((candidate for candidate in rows if identity(candidate) == item_id), None)
            yearly_values.append(
                {
                    "year": year,
                    "value": safe_float(row.get("value")) if row else 0.0,
                    "ms_pct": safe_float(row.get("ms_pct")) if row else 0.0,
                    "rank": row.get("rank") if row else None,
                }
            )
        trends.append(
            {
                label_key: item_id,
                "brand": latest.get("brand"),
                "company": latest.get("company"),
                "is_target": bool(latest.get("is_target")),
                "is_jw": bool(latest.get("is_jw")),
                "yearly_values": yearly_values,
            }
        )
    if others_ids:
        yearly_values = []
        for year in years:
            rows = ranked_rows(year)
            others = [row for row in rows if identity(row) not in selected_ids]
            displayed_ms = sum(safe_float(row.get("ms_pct")) or 0.0 for row in rows if identity(row) in selected_ids)
            yearly_values.append(
                {
                    "year": year,
                    "value": sum(safe_float(row.get("value")) or 0.0 for row in others),
                    "ms_pct": round(max(0.0, 100.0 - displayed_ms), 4),
                    "rank": None,
                }
            )
        trends.append(
            {
                label_key: "기타",
                "brand": "기타" if label_key == "brand" else None,
                "company": "기타" if label_key == "company" else None,
                "is_target": False,
                "is_jw": False,
                "is_others": True,
                "yearly_values": yearly_values,
            }
        )
    return trends


def row_identity(row: dict[str, Any] | None, label_key: str) -> str | None:
    if not row:
        return None
    return str(row.get(label_key) or row.get("brand") or row.get("company") or row.get("name"))


def _period_value_for_row(row: dict[str, Any], period: str) -> float:
    history = _metric_history(row)
    return _value_from_period_item(history.get(period))


def _target_rank_overrides(
    rows: list[dict[str, Any]],
    *,
    label_key: str,
    target_name: str | None,
    cache_key: Any = None,
) -> dict[int, dict[str, Any]]:
    """Build target rows from full sibling mart history when market ranking is top-N.

    The mart-level `brand_ranking_stacked` payload is intentionally trimmed to
    target + top 5 when the target is available. For broad ML views a target can
    fall outside that trimmed payload and later be reintroduced from catalog as a
    synthetic zero row. This helper restores the real target value/rank from the
    full sibling brand metric rows without changing mart or catalog definitions.
    """
    if not target_name:
        return {}
    stats_key = cache_key if cache_key is not None else id(rows)
    if stats_key not in TARGET_RANK_STATS_CACHE:
        annual_by_year, _ = _annual_rank_rows_from_full_rows(
            rows,
            label_key=label_key,
            target_name=target_name,
        )
        TARGET_RANK_STATS_CACHE[stats_key] = {
            year: {
                row_identity(row, label_key): {
                    "row": row,
                    "value": safe_float(row.get("value")) or 0.0,
                    "rank": row.get("rank"),
                    "ms_pct": safe_float(row.get("ms_pct")) or 0.0,
                    "is_jw": bool(row.get("is_jw")),
                    "company": row.get("company"),
                    "brand": row.get("brand"),
                }
                for row in year_rows
                if row_identity(row, label_key)
            }
            for year, year_rows in annual_by_year.items()
        }

    overrides: dict[int, dict[str, Any]] = {}
    for year, year_stats in TARGET_RANK_STATS_CACHE[stats_key].items():
        stat = year_stats.get(target_name)
        if not stat:
            continue
        overrides[year] = {
            label_key: target_name,
            "brand": stat.get("brand") or (target_name if label_key == "brand" else None),
            "company": stat.get("company") or (target_name if label_key == "company" else None),
            "is_target": True,
            "is_jw": bool(stat.get("is_jw")) or True,
            "is_others": False,
            "value": stat["value"],
            "rank": stat["rank"],
            "ms_pct": stat["ms_pct"],
        }
    return overrides


def _analysis_levels(level_top5: dict[str, Any], source: str) -> dict[str, Any]:
    levels = list((level_top5 or {}).keys())
    data = {}
    for level, period_map in (level_top5 or {}).items():
        latest_period = None
        latest = []
        if isinstance(period_map, dict):
            for period, rows in sorted(period_map.items(), reverse=True):
                if isinstance(rows, list) and rows:
                    latest_period = period
                    latest = rows
                    break
        total = sum(_row_value(row) for row in latest)
        segments = [
            {
                "name": row.get("label") or row.get("level") or row.get("name") or row.get(level),
                "rank": row.get("rank") or idx,
                "recent_share_pct": row.get("ms") or row.get("share_pct"),
                "series_pct": [(_row_share(row) if latest_period else 0.0)],
                "value_series": [_row_value(row)],
            }
            for idx, row in enumerate(latest, start=1)
        ]
        if total and not any(segment.get("recent_share_pct") for segment in segments):
            for segment in segments:
                segment["recent_share_pct"] = round((segment["value_series"][-1] / total) * 100, 4)
                segment["series_pct"] = [segment["recent_share_pct"]]
        data[level] = {"segments": segments, "by_channel": {"전체": segments}}
    return _normalize_segment_name_lists({
        "levels": levels,
        "channels": ["전체"] if levels else [],
        "period_unit": "monthly" if source == "UBIST" else "quarterly",
        "periods_monthly": [],
        "periods_quarterly": [],
        "data": data,
    })


def _series_from_period_map(period_map: dict[str, Any]) -> tuple[list[float], list[float]]:
    values: list[float] = []
    shares: list[float] = []
    for _, item in sorted((period_map or {}).items()):
        if isinstance(item, dict):
            values.append(_row_value(item))
            shares.append(_row_share(item))
        else:
            values.append(safe_float(item) or 0.0)
            shares.append(0.0)
    if values and not any(shares):
        total = sum(values)
        shares = [round(value / total * 100, 4) if total else 0.0 for value in values]
    return values, shares


def _normalize_analysis_levels(raw: Any, fallback_level_top5: dict[str, Any], source: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or "levels" in raw:
        normalized = raw if isinstance(raw, dict) and "levels" in raw else _analysis_levels(fallback_level_top5, source)
    else:
        levels = list(raw.keys())
        data = {}
        for level, segment_map in raw.items():
            segments = []
            if isinstance(segment_map, dict):
                ranked = []
                for name, period_map in segment_map.items():
                    if not isinstance(period_map, dict):
                        continue
                    values, shares = _series_from_period_map(period_map)
                    recent_value = values[-1] if values else 0.0
                    recent_share = shares[-1] if shares else 0.0
                    ranked.append((recent_value, name, values, shares, recent_share))
                for idx, (_, name, values, shares, recent_share) in enumerate(sorted(ranked, reverse=True)[:8], start=1):
                    segments.append(
                        {
                            "name": name,
                            "rank": idx,
                            "recent_share_pct": recent_share,
                            "series_pct": shares,
                            "value_series": values,
                        }
                    )
            data[level] = {"segments": segments, "by_channel": {"전체": segments}}
        normalized = {
            "levels": levels,
            "channels": ["전체"] if levels else [],
            "period_unit": "monthly" if source == "UBIST" else "quarterly",
            "periods_monthly": [],
            "periods_quarterly": [],
            "data": data,
        }

    for level in normalized.get("levels", []):
        level_data = normalized.setdefault("data", {}).setdefault(level, {})
        segments = level_data.get("segments") or []
        if not level_data.get("by_channel"):
            level_data["by_channel"] = {"전체": segments}
    if not normalized.get("channels") and normalized.get("levels"):
        normalized["channels"] = ["전체"]
    return _normalize_segment_name_lists(_filter_d3_levels(normalized))


def _filter_d3_levels(analysis_levels: dict[str, Any]) -> dict[str, Any]:
    """Apply PL D.3 level rules.

    D.3 is segment-level analysis, so Brand is removed because A.2 already owns
    brand ranking. Levels with one or zero options are also hidden because they
    do not create a meaningful dropdown comparison, even if the catalog flag is
    enabled for the market.
    """
    if not isinstance(analysis_levels, dict):
        return analysis_levels
    data = analysis_levels.get("data") if isinstance(analysis_levels.get("data"), dict) else {}
    kept_levels: list[str] = []
    kept_data: dict[str, Any] = {}
    for level in analysis_levels.get("levels") or []:
        if level == "Brand":
            continue
        level_data = data.get(level) or {}
        all_segments = level_data.get("by_channel", {}).get("전체") or level_data.get("segments") or []
        option_names = {segment.get("name") for segment in all_segments if isinstance(segment, dict) and segment.get("name")}
        if len(option_names) <= 1:
            continue
        kept_levels.append(level)
        kept_data[level] = level_data
    filtered = deepcopy(analysis_levels)
    filtered["levels"] = kept_levels
    filtered["data"] = kept_data
    if not kept_levels:
        filtered["channels"] = []
    return filtered


def _segment_names(segments: Any) -> list[str]:
    names: list[str] = []
    if not isinstance(segments, list):
        return names
    for segment in segments:
        if isinstance(segment, dict):
            name = segment.get("name")
        else:
            name = segment
        if name is not None:
            names.append(str(name))
    return names


def _normalize_segment_name_lists(analysis_levels: dict[str, Any]) -> dict[str, Any]:
    """Expose spec-facing segments as string[] while preserving by_channel rows."""
    if not isinstance(analysis_levels, dict):
        return analysis_levels
    data = analysis_levels.get("data")
    if not isinstance(data, dict):
        return analysis_levels
    for level_data in data.values():
        if not isinstance(level_data, dict):
            continue
        by_channel = level_data.get("by_channel")
        if isinstance(by_channel, dict) and isinstance(by_channel.get("전체"), list):
            level_data["segments"] = _segment_names(by_channel["전체"])
        else:
            level_data["segments"] = _segment_names(level_data.get("segments"))
    return analysis_levels


def _history_periods(rows: list[dict[str, Any]], source: str) -> list[str]:
    periods: set[str] = set()
    for row in rows:
        history = _metric_history(row)
        if history:
            periods.update(str(period) for period in history.keys())
    ordered = sorted(periods, key=period_key)
    return ordered[-60:] if source == "UBIST" else ordered[-20:]


def _period_unit_ko(source: str) -> str:
    return "월" if source == "UBIST" else "분기"


def _market_levels(market: dict[str, Any] | None) -> list[str]:
    market = market or {}
    levels: list[str] = []
    if bool(market.get("analyze_class")):
        levels.append("Class")
    if bool(market.get("analyze_molecule")):
        levels.append("Molecule")
    if bool(market.get("analyze_dosage_form")):
        levels.append("제형/투여경로")
    if bool(market.get("analyze_strength_pack")):
        levels.append("용량")
    if bool(market.get("analyze_nhi_type")):
        levels.append("비/급여")
    if bool(market.get("analyze_ox_gx")):
        levels.append("Ox/Gx")
    if bool(market.get("analyze_fish_oil")):
        levels.append(FISH_OIL_LEVEL)
    return levels


def _strategic_levels(market: dict[str, Any] | None, view_source_id: str | None) -> list[str]:
    levels = _market_levels(market)
    if _is_ml011_view(market, view_source_id) and "Class" in levels:
        index = levels.index("Class")
        levels[index : index + 1] = ["Class 1", "Class 2"]
    return levels


def _is_ml011_view(market: dict[str, Any] | None, view_source_id: str | None) -> bool:
    source_id = str(view_source_id or "")
    if source_id == "ml_011":
        return True
    market_id = str((market or {}).get("ml_id") or "")
    return market_id == "ml_011"


def _response_levels(market: dict[str, Any] | None, view_source_id: str | None) -> list[str]:
    """Return the v0.9.1 level keys that must always be visible in cause.

    Most markets expose the seven canonical levels. The existing ml_011
    Aktemra split is kept as-is because downstream Phase 30/31 checks already
    depend on Class 1/Class 2 being distinct instead of a single Class bucket.
    """
    if _is_ml011_view(market, view_source_id) and bool((market or {}).get("analyze_class")):
        levels = list(CAUSE_LEVELS_ML011)
    else:
        levels = list(CAUSE_LEVELS_V091)
    if bool((market or {}).get("analyze_fish_oil")) and FISH_OIL_LEVEL not in levels:
        levels.append(FISH_OIL_LEVEL)
    return levels


def _split_atomic_dimension(level: str, value: Any) -> list[str]:
    """Return display/selection atoms for a dimension value.

    Strength packs arrive from the mart as brand-level composites such as
    ``10mg | 20mg``. D.3 dropdowns must expose the individual strengths rather
    than the composite label, while other dimensions keep their catalog value.
    """
    if value in (None, "", [], {}):
        return []
    text = str(value)
    if level == "용량":
        return [part.strip() for part in text.split("|") if part.strip()]
    return [text]


def _dimension_value(row: dict[str, Any], level: str) -> str | None:
    values = _dimension_values(row, level)
    return values[0] if values else None


def _dimension_values(row: dict[str, Any], level: str) -> list[str]:
    if level == "Brand":
        value = row.get("brand_name") or row.get("brand_key")
        return _split_atomic_dimension(level, value)
    by_dimension = row.get("__by_dimension")
    if by_dimension is None:
        by_dimension = decode_json(row.get("by_dimension"))
        row["__by_dimension"] = by_dimension
    if not isinstance(by_dimension, dict):
        by_dimension = {}
    field = LEVEL_FIELD_BY_LABEL.get(level)
    candidates = [field] if field else []
    if level == "Class 2":
        candidates.extend(["class2", "class_2", "class_secondary", "class_sub"])
    if level == "Class 1":
        candidates.extend(["class1", "class_1", "class_primary"])
    for candidate in candidates:
        if not candidate:
            continue
        value = by_dimension.get(candidate)
        if value not in (None, "", [], {}):
            return _split_atomic_dimension(level, value)
    return []


def _channel_bucket(raw: Any, source: str) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if source == "UBIST":
        if "상급" in text:
            return "상급종병"
        if "종합" in text:
            return "종병"
        if text == "병원" or ("병원" in text and "치과" not in text):
            return "병원"
        if "의원" in text or "보건소" in text or "보건" in text:
            return "의원/보건소"
        return None
    upper = text.upper()
    if upper in {"KHPA", "KCPA", "KPA"}:
        return upper
    return None


def _channels_for_source(source: str) -> list[str]:
    return CHANNELS_5 if source == "UBIST" else IQVIA_CHANNELS


def _dual_channel_data(row: dict[str, Any], source: str, channel: str) -> dict[str, Any] | None:
    if source != "UBIST" or channel == "전체":
        return None
    channel_data = row.get("__ubist_dual_channel_data")
    if isinstance(channel_data, dict) and channel in channel_data:
        data = channel_data.get(channel)
        return data if isinstance(data, dict) else None
    return None


def _measure_labels(source: str) -> dict[str, str | None]:
    if source == "UBIST":
        return {"primary": "처방조제액", "secondary": "처방량"}
    return {"primary": "Sales", "secondary": "Units"}


def _value_from_period_item(item: Any) -> float:
    if isinstance(item, dict):
        return _row_value(item)
    return safe_float(item) or 0.0


def _add_series(target: dict[str, list[float]], series: dict[str, Any], periods: list[str]) -> None:
    for period in periods:
        target[period][0] += _value_from_period_item(series.get(period))


def _segment_rows_for_level(
    *,
    rows: list[dict[str, Any]],
    level: str,
    periods: list[str],
    source: str,
    channel: str,
    target_name: str | None,
    top_n: int | None = 5,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, list[float]]] = {}
    totals: dict[str, list[float]] = {period: [0.0] for period in periods}

    for row in rows:
        names = _dimension_values(row, level)
        if not names:
            continue
        for name in names:
            grouped.setdefault(name, {period: [0.0] for period in periods})
        if channel == "전체":
            history = _metric_history(row)
            if history:
                _add_series(totals, history, periods)
                for name in names:
                    _add_series(grouped[name], history, periods)
            continue

        dual_channel_data = _dual_channel_data(row, source, channel)
        if isinstance(dual_channel_data, dict):
            _add_series(totals, dual_channel_data, periods)
            for name in names:
                _add_series(grouped[name], dual_channel_data, periods)
            continue

        channel_data = row.get("__channel_data")
        if channel_data is None:
            channel_data = decode_json(row.get("channel_data"))
            row["__channel_data"] = channel_data
        if not isinstance(channel_data, dict):
            continue
        for raw_channel, series in channel_data.items():
            if _channel_bucket(raw_channel, source) != channel:
                continue
            if isinstance(series, dict):
                _add_series(totals, series, periods)
                for name in names:
                    _add_series(grouped[name], series, periods)

    ranked = sorted(
        grouped.items(),
        key=lambda item: item[1][periods[-1]][0] if periods else 0.0,
        reverse=True,
    )
    if target_name:
        ranked = sorted(ranked, key=lambda item: (item[0] != target_name, -(item[1][periods[-1]][0] if periods else 0.0)))
    selected = ranked if top_n is None else ranked[:top_n]

    segments: list[dict[str, Any]] = []
    for rank, (name, series_map) in enumerate(selected, start=1):
        value_series = [round(series_map[period][0], 4) for period in periods]
        series_pct = []
        for period, value in zip(periods, value_series):
            total = totals[period][0]
            series_pct.append(round(value / total * 100, 4) if total else 0.0)
        segments.append(
            {
                "name": name,
                "rank": rank,
                "recent_share_pct": series_pct[-1] if series_pct else 0.0,
                "series_pct": series_pct,
                "value_series": value_series,
            }
        )
    return segments


def _rows_for_channel(rows: list[dict[str, Any]], source: str, channel: str, periods: list[str]) -> list[dict[str, Any]]:
    if channel == "전체":
        return rows

    filtered: list[dict[str, Any]] = []
    for row in rows:
        history = {period: 0.0 for period in periods}
        dual_channel_data = _dual_channel_data(row, source, channel)
        if isinstance(dual_channel_data, dict):
            for period in periods:
                history[period] += _value_from_period_item(dual_channel_data.get(period))
        else:
            channel_data = row.get("__channel_data")
            if channel_data is None:
                channel_data = decode_json(row.get("channel_data"))
                row["__channel_data"] = channel_data
            if isinstance(channel_data, dict):
                for raw_channel, series in channel_data.items():
                    if _channel_bucket(raw_channel, source) != channel or not isinstance(series, dict):
                        continue
                    for period in periods:
                        history[period] += _value_from_period_item(series.get(period))

        clone = dict(row)
        clone["metric_history"] = history
        clone["__metric_history"] = history
        clone.pop("__latest_history_item", None)
        clone.pop("__series_cache", None)
        filtered.append(clone)
    return filtered


def _total_series_for_rows(rows: list[dict[str, Any]], periods: list[str]) -> list[float]:
    totals = [0.0 for _ in periods]
    for row in rows:
        series = _series_for_row(row, periods, scaled_sales=True)
        for idx, value in enumerate(series):
            totals[idx] += value
    return [round(value, 4) for value in totals]


def _build_analysis_levels_from_mart(
    *,
    rows: list[dict[str, Any]],
    source: str,
    market: dict[str, Any] | None,
    view_source_id: str | None,
    target_name: str | None,
    fallback_level_top5: dict[str, Any],
    channels_override: list[str] | None = None,
) -> dict[str, Any]:
    levels = _response_levels(market, view_source_id)
    enabled_levels = set(_strategic_levels(market, view_source_id))
    enabled_levels.add("Brand")
    periods = _history_periods(rows, source)
    data: dict[str, Any] = {}
    channels = channels_override or _channels_for_source(source)
    for level in levels:
        if level in enabled_levels:
            by_channel = {
                channel: _segment_rows_for_level(
                    rows=rows,
                    level=level,
                    periods=periods,
                    source=source,
                    channel=channel,
                    target_name=target_name if level == "Brand" else None,
                    top_n=None if channel == "전체" and level != "Brand" else 5,
                )
                for channel in channels
            }
        else:
            by_channel = {channel: [] for channel in channels}
        data[level] = {"segments": by_channel["전체"], "by_channel": by_channel}
    return _normalize_segment_name_lists({
        "levels": levels,
        "channels": channels,
        "period_unit": _period_unit_ko(source),
        "periods_monthly": periods if source == "UBIST" else [],
        "periods_quarterly": periods if source == "IQVIA" else [],
        "data": data,
    })


def _trim_analysis_levels(analysis_levels: dict[str, Any], limit: int = 5) -> dict[str, Any]:
    """Keep analysis-level payload compact for non-target competitor cache rows."""
    trimmed = deepcopy(analysis_levels)
    for level_data in (trimmed.get("data") or {}).values():
        if isinstance(level_data.get("segments"), list):
            level_data["segments"] = level_data["segments"][:limit]
        by_channel = level_data.get("by_channel")
        if isinstance(by_channel, dict):
            for channel, segments in list(by_channel.items()):
                if isinstance(segments, list):
                    by_channel[channel] = segments[:limit]
    return trimmed


def _growth_ms_matrix(ei_rows: Any) -> dict[str, Any]:
    rows = ei_rows if isinstance(ei_rows, list) else []
    output = []
    for row in rows:
        share = safe_float(row.get("ms") or row.get("share_pct"))
        contribution = safe_float(row.get("momentum_score") or row.get("growth_contribution") or row.get("contribution_pct"))
        output.append(
            {
                "brand": row.get("brand") or row.get("brand_key"),
                "company": row.get("company"),
                "is_target": bool(row.get("is_target")),
                "is_jw": bool(row.get("is_jw")),
                "share_pct": share,
                "contribution_pct": contribution,
                "growth_contribution": contribution,
                "value_recent": row.get("raw_value") or row.get("value"),
            }
        )
    shares = [row["share_pct"] for row in output if row["share_pct"] is not None]
    return {
        "data": output,
        "ms_avg_pct": round(sum(shares) / len(shares), 4) if shares else None,
        "share_avg_pct": round(sum(shares) / len(shares), 4) if shares else None,
    }


def _series_for_row(row: dict[str, Any], periods: list[str], *, scaled_sales: bool) -> list[float]:
    cache_key = (tuple(periods), scaled_sales)
    series_cache = row.setdefault("__series_cache", {})
    if cache_key in series_cache:
        return series_cache[cache_key]
    history = _metric_history(row)
    values = []
    for period in periods:
        value = _value_from_period_item(history.get(period))
        values.append(round(value, 4))
    series_cache[cache_key] = values
    return values


def _display_brand_rows(
    rows: list[dict[str, Any]],
    *,
    target_name: str | None,
    top_n: int = 5,
    include_others: bool,
    market_series: dict[str, Any] | None = None,
    ei_market_key: Any = None,
) -> list[dict[str, Any]]:
    def first_float(*values: Any) -> float | None:
        for value in values:
            parsed = optional_float(value)
            if parsed is not None:
                return parsed
        return None

    normalized: list[dict[str, Any]] = []
    for row in rows:
        brand = _row_brand(row)
        if not brand:
            continue
        recent = _latest_history_item(row)
        extended = _latest_extended_item(row)
        is_target = bool(target_name and brand == target_name)
        value_recent = safe_float(recent.get("raw_value") or recent.get("value")) or 0.0
        share = safe_float(recent.get("ms")) or 0.0
        cache_key = (ei_market_key if ei_market_key is not None else id(market_series), row.get("id") or row.get("brand_key") or brand)
        if cache_key not in EI_META_CACHE:
            EI_META_CACHE[cache_key] = calculate_ei_with_fallback(_metric_history(row), market_series)
        ei_meta = EI_META_CACHE[cache_key]
        cagr_5y = first_float(extended.get("cagr_5y"))
        cagr_5y_pct = round(cagr_5y * 100, 4) if cagr_5y is not None else None
        ei_5y = optional_float(ei_meta.get("ei"))
        momentum_score = first_float(extended.get("momentum_score"))
        growth_contribution = safe_float(
            extended.get("growth_contribution")
            or extended.get("growth_contribution_pct")
            or extended.get("momentum_score")
        ) or 0.0
        normalized.append(
            {
                "brand": brand,
                "brand_key": row.get("brand_key") or brand,
                "company": _row_company(row),
                "is_target": is_target,
                "is_jw": bool(row.get("is_jw")) or is_target,
                "is_others": False,
                "rank": recent.get("rank"),
                "rank_overall": recent.get("rank"),
                "value_recent": value_recent,
                "raw_value": value_recent,
                "share_pct": share,
                "ms_pct": share,
                "ms_recent_pct": share,
                "ei": ei_5y,
                "ei_5y": ei_5y,
                "cagr_5y_pct": cagr_5y_pct,
                "brand_cagr_pct": optional_float(ei_meta.get("brand_cagr_pct")),
                "market_cagr_pct": optional_float(ei_meta.get("market_cagr_pct")),
                "ei_basis": ei_meta.get("basis"),
                "ei_period_years": ei_meta.get("period_years"),
                "ei_note": ei_meta.get("note"),
                "cagr_basis": ei_meta.get("basis"),
                "momentum_score": momentum_score,
                "growth_contribution": growth_contribution,
                "growth_contribution_pct": growth_contribution,
                "contribution": growth_contribution,
                "contribution_pct": growth_contribution,
                "_source_row": row,
            }
        )

    market_total = optional_float(series_latest_number(market_series)) if market_series else None
    if market_total is None or market_total <= 0:
        market_total = sum(row["value_recent"] for row in normalized)
    if market_total and market_total > 0:
        for row in normalized:
            share = round(row["value_recent"] / market_total * 100, 4)
            row["share_pct"] = share
            row["ms_pct"] = share
            row["ms_recent_pct"] = share

    ranked = [
        row
        for row in sorted(normalized, key=lambda item: item["value_recent"], reverse=True)
        if row["value_recent"] > 0
    ]
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index

    target = next((row for row in normalized if row["is_target"]), None)
    target_id = row_identity(target, "brand")
    competitors = [
        row
        for row in sorted(normalized, key=lambda item: item["value_recent"], reverse=True)
        if row_identity(row, "brand") != target_id
    ]
    selected = ([target] if target else []) + competitors[:top_n]
    selected_ids = {row_identity(row, "brand") for row in selected}
    others = [row for row in normalized if row_identity(row, "brand") not in selected_ids]
    if include_others and others:
        selected_ms = sum(row["ms_pct"] for row in selected)
        selected_contribution = sum(row["contribution_pct"] for row in selected)
        selected.append(
            {
                "brand": "기타",
                "brand_key": "기타",
                "company": f"{len(others)}개 brand",
                "is_target": False,
                "is_jw": False,
                "is_others": True,
                "rank": None,
                "rank_overall": None,
                "value_recent": sum(row["value_recent"] for row in others),
                "raw_value": sum(row["raw_value"] for row in others),
                "share_pct": round(max(0.0, 100.0 - selected_ms), 4),
                "ms_pct": round(max(0.0, 100.0 - selected_ms), 4),
                "ms_recent_pct": round(max(0.0, 100.0 - selected_ms), 4),
                "ei": None,
                "ei_5y": None,
                "cagr_5y_pct": None,
                "brand_cagr_pct": None,
                "market_cagr_pct": None,
                "ei_basis": None,
                "ei_period_years": None,
                "ei_note": None,
                "cagr_basis": None,
                "momentum_score": None,
                "growth_contribution": sum(row["growth_contribution"] for row in others),
                "growth_contribution_pct": round(100.0 - selected_contribution, 4),
                "contribution": sum(row["contribution"] for row in others),
                "contribution_pct": round(100.0 - selected_contribution, 4),
            }
        )
    return [{key: value for key, value in row.items() if key != "_source_row"} for row in selected]


def _matrix_payload(entries: list[dict[str, Any]]) -> dict[str, Any]:
    visible = [row for row in entries if not row.get("is_others")]
    shares = [safe_float(row.get("share_pct")) or 0.0 for row in visible]
    avg = round(sum(shares) / len(shares), 4) if shares else 0.0
    return {"data": entries, "ms_avg_pct": avg, "share_avg_pct": avg}


def _annual_latest_points(period_map: Any, *, value_key: str) -> list[dict[str, Any]]:
    if isinstance(period_map, list):
        points = [point for point in period_map if isinstance(point, dict)]
        return points[-5:]
    if not isinstance(period_map, dict):
        return []
    by_year: dict[int, tuple[str, Any]] = {}
    for period, item in sorted(period_map.items(), key=lambda pair: period_key(str(pair[0]))):
        year = _period_year(str(period))
        if year is not None:
            by_year[year] = (str(period), item)
    points = []
    for year in sorted(by_year.keys())[-5:]:
        period, item = by_year[year]
        if isinstance(item, dict):
            value = safe_float(item.get(value_key) or item.get("hhi") or item.get("company_hhi") or item.get("cr4"))
        else:
            value = safe_float(item)
        points.append({"period": period, "period_full": period, "year": year, value_key: value or 0.0})
    return points


def _annual_share_hhi(period_map: Any) -> list[dict[str, Any]]:
    """Recalculate yearly HHI from annual summed rows.

    Phase H uses annual sums rather than each year's latest period snapshot.
    Rows may come from mart ranking payloads and can use either brand/name plus
    sales/value/raw_value keys.
    """
    if not isinstance(period_map, dict):
        return []
    by_year: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    period_by_year: dict[int, str] = {}
    for period, rows in sorted(period_map.items(), key=lambda pair: period_key(str(pair[0]))):
        year = _period_year(str(period))
        if year is None or not isinstance(rows, list):
            continue
        period_by_year[year] = str(period)
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("brand") or row.get("name") or row.get("company") or row.get("brand_key")
            if not name:
                continue
            by_year[year][str(name)] += _row_value(row)
    points = []
    for year in sorted(by_year.keys())[-5:]:
        values = by_year[year]
        total = sum(values.values())
        hhi = sum(((value / total) * 100.0) ** 2 for value in values.values()) if total > 0 else 0.0
        points.append({"period": str(year), "period_full": period_by_year.get(year, str(year)), "year": year, "hhi": round(hhi, 4)})
    return points


def _annual_share_hhi_from_rows(rows: list[dict[str, Any]], *, label_key: str) -> list[dict[str, Any]]:
    by_year, _ = _annual_rank_rows({}, label_key=label_key, target_name=None, full_rows=rows)
    points = []
    for year in sorted(by_year.keys())[-5:]:
        year_rows = by_year[year]
        hhi = sum((safe_float(row.get("ms_pct")) or 0.0) ** 2 for row in year_rows)
        points.append({"period": str(year), "period_full": str(year), "year": year, "hhi": round(hhi, 4)})
    return points


def _company_hhi_from_ranking(company_ranking: Any) -> dict[str, Any]:
    if not isinstance(company_ranking, dict):
        return {"periods": [], "hhi_values": []}
    by_year: dict[int, tuple[str, list[dict[str, Any]]]] = {}
    for period, rows in sorted(company_ranking.items(), key=lambda pair: period_key(str(pair[0]))):
        year = _period_year(str(period))
        if year is not None and isinstance(rows, list):
            by_year[year] = (str(period), rows)
    periods: list[str] = []
    values: list[float] = []
    for year in sorted(by_year.keys())[-5:]:
        _, rows = by_year[year]
        hhi = sum((_row_share(row) ** 2) for row in rows)
        periods.append(str(year))
        values.append(round(hhi, 4))
    return {"periods": periods, "hhi_values": values}


def _company_hhi_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    points = _annual_share_hhi_from_rows(rows, label_key="company")
    return {
        "periods": [str(point["year"]) for point in points],
        "hhi_values": [round(safe_float(point.get("hhi")) or 0.0, 4) for point in points],
    }


def _data_period_coverage(period_map: dict[str, Any], *, source: str) -> dict[str, Any]:
    periods = sorted((str(period) for period in (period_map or {}).keys()), key=period_key)
    by_year: dict[str, int] = defaultdict(int)
    for period in periods:
        year = str(period)[:4]
        if year:
            by_year[year] += 1
    latest_period = periods[-1] if periods else None
    latest_year = str(latest_period)[:4] if latest_period else None
    expected = 12 if source == "UBIST" else 4
    latest_count = by_year.get(latest_year, 0) if latest_year else 0
    return {
        "latest_period": latest_period,
        "latest_year": int(latest_year) if latest_year and latest_year.isdigit() else None,
        "latest_year_period_count": latest_count,
        "latest_year_is_partial": bool(latest_year and latest_count < expected),
        "period_count_by_year": dict(by_year),
        "expected_periods_per_year": expected,
    }


def _company_waterfall(entries: list[dict[str, Any]], *, target_company: str | None) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in entries:
        company = row.get("company") or row.get("brand") or "Unknown"
        bucket = grouped.setdefault(
            company,
            {
                "company": company,
                "brands": [],
                "is_target": bool(target_company and company == target_company),
                "is_jw": False,
                "contribution": 0.0,
                "contribution_pct": 0.0,
                "value_recent": 0.0,
            },
        )
        bucket["brands"].append(row.get("brand"))
        bucket["is_target"] = bucket["is_target"] or bool(target_company and company == target_company)
        bucket["is_jw"] = bucket["is_jw"] or bool(row.get("is_jw"))
        bucket["contribution"] += safe_float(row.get("growth_contribution")) or 0.0
        bucket["contribution_pct"] += safe_float(row.get("growth_contribution_pct")) or 0.0
        bucket["value_recent"] += safe_float(row.get("value_recent")) or 0.0
    rows = list(grouped.values())
    target = next((row for row in rows if row["is_target"]), None)
    competitors = [row for row in sorted(rows, key=lambda item: item["value_recent"], reverse=True) if row is not target]
    selected = ([target] if target else []) + competitors[:5]
    rest = [row for row in rows if row not in selected]
    if rest:
        selected.append(
            {
                "company": "기타",
                "brands": [brand for row in rest for brand in row.get("brands", [])],
                "is_target": False,
                "is_jw": False,
                "is_others": True,
                "contribution": sum(row["contribution"] for row in rest),
                "contribution_pct": sum(row["contribution_pct"] for row in rest),
                "value_recent": sum(row["value_recent"] for row in rest),
            }
        )
    return {"top_contributors": selected, "others_total": 0.0}


def _history_value_at(row: dict[str, Any], period: str | None) -> float:
    if not period:
        return 0.0
    history = _metric_history(row)
    return _value_from_period_item((history or {}).get(period)) if isinstance(history, dict) else 0.0


def _top_contribution_rows(rows: list[dict[str, Any]], target_name: str | None, periods: list[str], top_n: int = 5) -> tuple[list[dict[str, Any]], float, float, float]:
    period_start = periods[0] if periods else None
    period_end = periods[-1] if periods else None
    market_start = sum(_history_value_at(row, period_start) for row in rows)
    market_end = sum(_history_value_at(row, period_end) for row in rows)
    market_growth = market_end - market_start
    contribution_rows: list[dict[str, Any]] = []
    for row in rows:
        brand = _row_brand(row)
        if not brand:
            continue
        start_value = _history_value_at(row, period_start)
        end_value = _history_value_at(row, period_end)
        value = end_value - start_value
        pct = round(value / market_growth * 100, 4) if market_growth else None
        contribution_rows.append(
            {
                "brand": brand,
                "company": _company_name(row),
                "is_target": bool(target_name and brand == target_name),
                "is_jw": bool(row.get("is_jw")) or bool(target_name and brand == target_name),
                "is_others": False,
                "contribution": value,
                "contribution_value": value,
                "contribution_pct": pct,
                "value_start": start_value,
                "value_end": end_value,
                "value_recent": end_value,
            }
        )

    target = next((row for row in contribution_rows if row["is_target"]), None)
    competitors = [row for row in sorted(contribution_rows, key=lambda item: abs(item["contribution_value"]), reverse=True) if row is not target]
    selected = ([target] if target else []) + competitors[:top_n]
    rest = [row for row in contribution_rows if row not in selected]
    if rest:
        displayed_pct = sum((row.get("contribution_pct") or 0.0) for row in selected)
        selected.append(
            {
                "brand": "기타",
                "company": f"{len(rest)}개 brand",
                "is_target": False,
                "is_jw": False,
                "is_others": True,
                "contribution": sum(row["contribution_value"] for row in rest),
                "contribution_value": sum(row["contribution_value"] for row in rest),
                "contribution_pct": round(100.0 - displayed_pct, 4) if market_growth else None,
                "value_start": sum(row["value_start"] for row in rest),
                "value_end": sum(row["value_end"] for row in rest),
                "value_recent": sum(row["value_recent"] for row in rest),
            }
        )
    return selected, market_start, market_end, market_growth


def _company_contribution_payload(rows: list[dict[str, Any]], target_company: str | None, periods: list[str], market_growth: float, top_n: int = 5) -> dict[str, Any]:
    period_start = periods[0] if periods else None
    period_end = periods[-1] if periods else None
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        company = _company_name(row)
        bucket = grouped.setdefault(company, {"company": company, "brands": [], "value_start": 0.0, "value_end": 0.0, "is_target": bool(target_company and company == target_company), "is_jw": False})
        bucket["brands"].append(_row_brand(row))
        bucket["value_start"] += _history_value_at(row, period_start)
        bucket["value_end"] += _history_value_at(row, period_end)
        bucket["is_jw"] = bucket["is_jw"] or bool(row.get("is_jw"))
    company_rows = []
    for bucket in grouped.values():
        value = bucket["value_end"] - bucket["value_start"]
        company_rows.append(
            {
                "company": bucket["company"],
                "brands": bucket["brands"],
                "is_target": bucket["is_target"],
                "is_jw": bucket["is_jw"],
                "is_others": False,
                "contribution": value,
                "contribution_value": value,
                "contribution_pct": round(value / market_growth * 100, 4) if market_growth else None,
                "value_recent": bucket["value_end"],
            }
        )
    target = next((row for row in company_rows if row["is_target"]), None)
    competitors = [row for row in sorted(company_rows, key=lambda item: abs(item["contribution_value"]), reverse=True) if row is not target]
    selected = ([target] if target else []) + competitors[:top_n]
    rest = [row for row in company_rows if row not in selected]
    if rest:
        displayed_pct = sum((row.get("contribution_pct") or 0.0) for row in selected)
        selected.append(
            {
                "company": "기타",
                "brands": [brand for row in rest for brand in row.get("brands", [])],
                "is_target": False,
                "is_jw": False,
                "is_others": True,
                "contribution": sum(row["contribution_value"] for row in rest),
                "contribution_value": sum(row["contribution_value"] for row in rest),
                "contribution_pct": round(100.0 - displayed_pct, 4) if market_growth else None,
                "value_recent": sum(row["value_recent"] for row in rest),
            }
        )
    return {"top_contributors": selected, "others_total": 0.0}


def _growth_contribution_base_payload(rows: list[dict[str, Any]], target_name: str | None, periods: list[str]) -> dict[str, Any]:
    top_rows, market_start, market_end, market_growth = _top_contribution_rows(rows, target_name, periods)
    by_brand = {
        "top_contributors": top_rows,
        "others_total": 0.0,
    }
    target_company = next((row.get("company") for row in top_rows if row.get("is_target")), None)
    return {
        "period_start": periods[0] if periods else None,
        "period_end": periods[-1] if periods else None,
        "market_start": market_start,
        "market_end": market_end,
        "market_growth": market_growth,
        "by_brand": by_brand,
        "by_company": _company_contribution_payload(rows, target_company=target_company, periods=periods, market_growth=market_growth),
    }


def _growth_window_periods(periods: list[str], source: str | None, n_years: int) -> list[str]:
    if not periods:
        return []
    stride = 12 if source == "UBIST" else 4
    start_idx = len(periods) - (stride * n_years)
    if start_idx < 0:
        return []
    return [periods[start_idx], periods[-1]]


def _growth_contribution_payload(rows: list[dict[str, Any]], target_name: str | None, periods: list[str], source: str | None = None) -> dict[str, Any]:
    payload = _growth_contribution_base_payload(rows, target_name, periods)
    windows: dict[str, dict[str, Any]] = {}
    for n_years in range(1, 5):
        window_periods = _growth_window_periods(periods, source, n_years)
        if window_periods:
            windows[f"{n_years}y"] = _growth_contribution_base_payload(rows, target_name, window_periods)
    windows["5y"] = deepcopy(payload)
    payload["windows"] = windows
    return payload


def _channel_data_quality(channel: str, periods: list[str], total_series: list[float]) -> dict[str, Any]:
    """Summarize channel history completeness without imputing missing source data."""
    nonzero_indexes = [idx for idx, value in enumerate(total_series) if value and value > 0]
    first_nonzero = periods[nonzero_indexes[0]] if nonzero_indexes else None
    note = None
    if periods and len(nonzero_indexes) < len(periods):
        note = (
            f"{channel} channel has source data from {first_nonzero} only; "
            "earlier periods are preserved as 0 and were not imputed."
            if first_nonzero
            else f"{channel} channel has no non-zero source data in the displayed window."
        )
    return {
        "period_count": len(periods),
        "nonzero_period_count": len(nonzero_indexes),
        "first_nonzero_period": first_nonzero,
        "note": note,
    }


def _target_customer_competition(
    *,
    rows: list[dict[str, Any]],
    source: str,
    target_name: str | None,
    periods: list[str],
    channels: list[str] | None = None,
) -> dict[str, Any]:
    targets = channels or _channels_for_source(source)
    target_type = "채널"
    period_tail = periods[-10:]
    views = []
    for target in targets:
        channel_rows = _rows_for_channel(rows, source, target, periods)
        selected = _display_brand_rows(channel_rows, target_name=target_name, top_n=5, include_others=True)
        row_by_brand = {_row_brand(row): row for row in channel_rows if _row_brand(row)}
        total_series = _total_series_for_rows(channel_rows, period_tail)
        selected_series: list[list[float]] = []
        trend_brands = []
        composition = []
        for item in selected:
            source_row = row_by_brand.get(item.get("brand"))
            if item.get("is_others"):
                value_series = [
                    round(
                        max(
                            0.0,
                            total - sum(series[idx] if idx < len(series) else 0.0 for series in selected_series),
                        ),
                        4,
                    )
                    for idx, total in enumerate(total_series)
                ]
            else:
                value_series = _series_for_row(source_row or {}, period_tail, scaled_sales=True) if source_row else [0.0] * len(period_tail)
                selected_series.append(value_series)
            trend_brands.append(
                {
                    "brand": item.get("brand"),
                    "company": item.get("company"),
                    "is_target": item.get("is_target"),
                    "is_jw": item.get("is_jw"),
                    "is_others": item.get("is_others"),
                    "rank": item.get("rank"),
                    "value_series": value_series,
                    "volume_series": value_series,
                }
            )
            composition.append(
                {
                    "brand": item.get("brand"),
                    "is_target": item.get("is_target"),
                    "is_jw": item.get("is_jw"),
                    "is_others": item.get("is_others"),
                    "pct": safe_float(item.get("share_pct")) or 0.0,
                }
            )
        views.append(
            {
                "target_name": target,
                "target_type": target_type,
                "periods": period_tail,
                "trend_brands": trend_brands,
                "composition": composition,
                "composition_volume": composition,
                "data_quality": _channel_data_quality(target, period_tail, total_series),
            }
        )
    return {
        "available_in_view": ["market_landscape", "competitive_dynamics"],
        "target_type": target_type,
        "targets": targets,
        "note": f"{source} {target_type} 기준 top 5 + 기타",
        "views": views,
    }


def _level_rows_by_segment(rows: list[dict[str, Any]], levels: list[str]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    rows_by_level: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for level in levels:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            for segment_name in _dimension_values(row, level):
                grouped.setdefault(segment_name, []).append(row)
        rows_by_level[level] = grouped
    return rows_by_level


def _level_top5_trend(
    analysis_levels: dict[str, Any],
    rows: list[dict[str, Any]],
    source: str,
    target_name: str | None,
    *,
    rows_by_level: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
    include_all_options: bool = False,
) -> dict[str, Any]:
    levels = analysis_levels.get("levels") or []
    periods = (analysis_levels.get("periods_monthly") or analysis_levels.get("periods_quarterly") or [])[-10:]
    available_levels = [{"key": level, "label": level} for level in levels]
    by_level = {}
    rows_by_level = rows_by_level or _level_rows_by_segment(rows, levels)
    for level in levels:
        all_level_segments = analysis_levels.get("data", {}).get(level, {}).get("by_channel", {}).get("전체") or []
        level_segments = all_level_segments if include_all_options else all_level_segments[:5]
        values = []
        for index, segment in enumerate(level_segments, start=1):
            segment_name = segment.get("name") or f"{level} {index}"
            segment_rows = rows_by_level.get(level, {}).get(segment_name, [])
            segment_brand_entries = _display_brand_rows(
                segment_rows,
                target_name=target_name,
                top_n=5,
                include_others=True,
            ) if segment_rows else []
            segment_row_by_brand = {_row_brand(row): row for row in segment_rows if _row_brand(row)}
            segment_total_series = segment.get("value_series") or _total_series_for_rows(segment_rows, periods)
            if len(segment_total_series) != len(periods):
                segment_total_series = list(segment_total_series)[-len(periods):] if periods else []
            selected_series: list[list[float]] = []
            brands_in_value = []
            for entry in segment_brand_entries:
                source_row = segment_row_by_brand.get(entry.get("brand"))
                if entry.get("is_others"):
                    series = [
                        round(
                            max(
                                0.0,
                                total - sum(item[idx] if idx < len(item) else 0.0 for item in selected_series),
                            ),
                            4,
                        )
                        for idx, total in enumerate(segment_total_series)
                    ]
                else:
                    series = _series_for_row(source_row or {}, periods, scaled_sales=True) if source_row else [0.0] * len(periods)
                    selected_series.append(series)
                brands_in_value.append(
                    {
                        "brand": entry.get("brand"),
                        "company": entry.get("company"),
                        "is_target": entry.get("is_target"),
                        "is_jw": entry.get("is_jw"),
                        "is_others": entry.get("is_others"),
                        "rank": entry.get("rank"),
                        "ms_recent_pct": safe_float(entry.get("share_pct")) or 0.0,
                        "value_recent": safe_float(entry.get("value_recent")) or 0.0,
                        "raw_value": safe_float(entry.get("raw_value")) or safe_float(entry.get("value_recent")) or 0.0,
                        "value_recent_100m": round((safe_float(entry.get("value_recent")) or 0.0) / 100_000_000, 4),
                        "volume_recent": safe_float(entry.get("value_recent")) or 0.0,
                        "value_series_10pt": series,
                        "ms_series_10pt": [
                            round(value / total * 100, 4) if total else 0.0
                            for value, total in zip(series, segment_total_series)
                        ],
                        "volume_series_10pt": series,
                    }
                )
            total_value = safe_float(segment_total_series[-1] if segment_total_series else None) or 0.0
            values.append(
                {
                    "value": segment_name,
                    "is_default": index == 1,
                    "total_value": total_value,
                    "total_volume": total_value,
                    "ms_pct": safe_float(segment.get("recent_share_pct")) or 0.0,
                    "brands_in_value": brands_in_value,
                }
            )
        by_level[level] = {
            "level_label": level,
            "level_value": values[0]["value"] if values else None,
            "default_value": values[0]["value"] if values else None,
            "total_market_value": sum((safe_float(item.get("total_value")) or 0.0) for item in values),
            "empty": not bool(values),
            "periods_10pt": periods,
            "all_options": [segment.get("name") for segment in level_segments if segment.get("name")],
            "default_option": values[0]["value"] if values else None,
            "values": values,
        }
    return {
        "available_levels": available_levels,
        "default_level": levels[0] if levels else None,
        "by_level": by_level,
        "note": "각 분석 level top 5 + 기타",
    }


def _catalog_members_for_market(strategic_brand: Any, view_source_id: str) -> list[dict[str, Any]]:
    if strategic_brand is None:
        return []
    if view_source_id.startswith("ml_"):
        sub = strategic_brand[strategic_brand["ml_id"].astype(str) == view_source_id]
    elif view_source_id.startswith("cd_") and "cd_id" in strategic_brand.columns:
        sub = strategic_brand[strategic_brand["cd_id"].astype(str) == view_source_id]
    else:
        return []
    members = []
    for _, row in sub.iterrows():
        name = str(row.get("canonical_name") or row.get("name") or "")
        if name:
            members.append({"name": name, "is_jw": bool(row.get("is_jw")), "company": row.get("판매사")})
    return members


def market_size_series_with_yoy(series: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    yoy_series = market_yoy_series(series)
    output: dict[str, dict[str, float | None]] = {}
    if not isinstance(series, dict):
        return output
    for period in sorted(series.keys(), key=period_key):
        value = safe_float(series.get(period))
        output[str(period)] = {
            "value": value,
            "yoy_growth_pct": yoy_series.get(str(period)),
        }
    return output


def latest_market_series_payload(series: dict[str, Any]) -> dict[str, Any]:
    yoy_series = market_yoy_series(series)
    return {
        "periods_unit": "월간",
        "periods_count": len(series or {}),
        "market_size_series": market_size_series_with_yoy(series),
        "market_yoy_series": yoy_series,
        "market_yoy_recent_pct": series_latest_number(yoy_series),
    }


def market_yoy_series(series: dict[str, Any]) -> dict[str, float | None]:
    if not isinstance(series, dict):
        return {}
    periods = sorted(series.keys(), key=period_key)
    step = 12 if any("-Q" not in str(period) for period in periods) else 4
    result: dict[str, float | None] = {}
    for index, period in enumerate(periods):
        current = safe_float(series.get(period))
        previous = safe_float(series.get(periods[index - step])) if index >= step else None
        if current is None or previous in (None, 0):
            result[str(period)] = None
        else:
            result[str(period)] = round((current - previous) / previous * 100, 4)
    return result


def top3_share(rows: list[dict[str, Any]]) -> float | None:
    shares = []
    for row in rows:
        recent = metric_recent(_metric_history(row))
        shares.append(safe_float(recent.get("ms")))
    if not shares:
        return None
    return round(sum(sorted(shares, reverse=True)[:3]), 2)


def choose_target(rows: list[dict[str, Any]], fallback: dict[str, Any]) -> dict[str, Any]:
    for row in rows:
        if bool(row.get("is_target")):
            return row
    for row in rows:
        if bool(row.get("is_jw")):
            return row
    return fallback


def build_response(
    *,
    brand_row: dict[str, Any],
    market_row: dict[str, Any],
    sibling_rows: list[dict[str, Any]],
    view_type: str,
    market_id: str,
    source: str,
    measure: str,
    view_source_id: str,
    market_name: str | None,
    market_sources: list[str],
    market_catalog_row: dict[str, Any] | None = None,
    strategic_brand: Any = None,
) -> dict[str, Any]:
    metric_history = decode_json(brand_row.get("metric_history"))
    extended = decode_json(brand_row.get("extended_metric_history"))
    recent = metric_recent(metric_history)
    ext_recent = metric_recent(extended)
    market_series = decode_json(market_row.get("market_size_series"))
    hhi_series = decode_json(market_row.get("hhi_series_5y") or market_row.get("hhi_series"))
    hhi_recent = series_latest_number(hhi_series)
    source_api = source
    target = choose_target(sibling_rows, brand_row)
    target_company_name = target.get("company_name") or _row_company(target)
    target_recent = metric_recent(decode_json(target.get("metric_history")))
    target_ext = metric_recent(decode_json(target.get("extended_metric_history")))

    brand_ranking = decode_json(market_row.get("brand_ranking_stacked"))
    company_ranking = decode_json(market_row.get("company_ranking_stacked"))
    level_top5 = decode_json(market_row.get("level_top5_trend"))
    catalog_members = _catalog_members_for_market(strategic_brand, view_source_id)
    analysis_view_id = view_source_id
    analysis_cache_key = (analysis_view_id, source_api, measure)
    ubist_channel_context = (
        resolve_market_channels(rows=sibling_rows, market=market_catalog_row, measure=measure)
        if source_api == "UBIST"
        else None
    )
    channels_override = (
        ubist_channel_context.get("channels")
        if isinstance(ubist_channel_context, dict) and ubist_channel_context.get("channels")
        else None
    )
    if analysis_cache_key not in ANALYSIS_LEVELS_CACHE:
        ANALYSIS_LEVELS_CACHE[analysis_cache_key] = _build_analysis_levels_from_mart(
            rows=sibling_rows,
            source=source_api,
            market=market_catalog_row,
            view_source_id=analysis_view_id,
            target_name=None,
            fallback_level_top5=level_top5,
            channels_override=channels_override,
        )
    analysis_levels = deepcopy(ANALYSIS_LEVELS_CACHE[analysis_cache_key])
    if analysis_cache_key not in LEVEL_ROW_GROUPS_CACHE:
        LEVEL_ROW_GROUPS_CACHE[analysis_cache_key] = _level_rows_by_segment(
            sibling_rows,
            ANALYSIS_LEVELS_CACHE[analysis_cache_key].get("levels") or [],
        )
    include_all_d3_options = bool(brand_row.get("is_jw") or brand_row.get("is_target"))
    if not include_all_d3_options:
        analysis_levels = _trim_analysis_levels(analysis_levels)
    brand_ranking_stacked = _stacked_ranking(
        brand_ranking,
        label_key="brand",
        target_name=brand_row.get("brand_name"),
        catalog_members=catalog_members,
        full_rows=sibling_rows,
        target_overrides=_target_rank_overrides(
            sibling_rows,
            label_key="brand",
            target_name=brand_row.get("brand_name"),
            cache_key=(view_source_id, source_api, measure),
        ),
    )
    company_ranking_stacked = _stacked_ranking(
        company_ranking,
        label_key="company",
        target_name=target_company_name,
        full_rows=sibling_rows,
        target_overrides=_target_rank_overrides(
            sibling_rows,
            label_key="company",
            target_name=target_company_name,
            cache_key=("company", view_source_id, source_api, measure),
        ),
    )
    display_entries_no_others = _display_brand_rows(
        sibling_rows,
        target_name=brand_row.get("brand_name"),
        top_n=5,
        include_others=False,
        market_series=market_series,
        ei_market_key=market_row.get("id"),
    )
    display_entries_with_others = _display_brand_rows(
        sibling_rows,
        target_name=brand_row.get("brand_name"),
        top_n=5,
        include_others=True,
        market_series=market_series,
        ei_market_key=market_row.get("id"),
    )
    target_display = next((row for row in display_entries_no_others if row.get("is_target")), {})
    periods = _history_periods(sibling_rows, source_api)
    hhi_points = _annual_share_hhi_from_rows(sibling_rows, label_key="brand")
    company_concentration = _company_hhi_from_rows(sibling_rows)
    data_period_coverage = _data_period_coverage(market_series, source=source_api)
    growth_contribution = _growth_contribution_payload(sibling_rows, brand_row.get("brand_name"), periods, source=source_api)
    target_customer_competition = _target_customer_competition(
        rows=sibling_rows,
        source=source_api,
        target_name=brand_row.get("brand_name"),
        periods=periods,
        channels=analysis_levels.get("channels"),
    )
    level_top5_trend = _level_top5_trend(
        analysis_levels,
        sibling_rows,
        source_api,
        brand_row.get("brand_name"),
        rows_by_level=LEVEL_ROW_GROUPS_CACHE[analysis_cache_key],
        include_all_options=include_all_d3_options,
    )
    direct_competition_count = max(
        len({r.get("brand_key") for r in sibling_rows if r.get("brand_key")}),
        len({member["name"] for member in catalog_members if member.get("name")}),
    )

    return {
        "brand": brand_row["brand_name"],
        "brand_name": brand_row["brand_name"],
        "brand_key": brand_row["brand_key"],
        "market_id": market_id,
        "view": view_type,
        "source": source,
        "measure": measure,
        "unit_label": brand_row.get("unit_label"),
        "data": {
            "kpi": {
                "market_size_recent": series_latest_number(market_series),
                "market_cagr_5y_pct": series_cagr(market_series),
                "top3_share_pct": top3_share(sibling_rows),
                "hhi_recent": hhi_recent,
                "direct_competition_count": direct_competition_count,
                "target_brand": target.get("brand_name"),
                "target_company": target_company_name or ("JW중외제약" if target.get("is_jw") else None),
                "target_ei": optional_float(target_display.get("ei")),
                "ei": optional_float(target_display.get("ei")),
                "ei_basis": target_display.get("ei_basis"),
                "ei_period_years": target_display.get("ei_period_years"),
                "ei_note": target_display.get("ei_note"),
                "brand_cagr_pct": optional_float(target_display.get("brand_cagr_pct")),
                "market_cagr_pct": optional_float(target_display.get("market_cagr_pct")),
                "target_momentum": optional_float(target_display.get("momentum_score")),
                "target_rank": target_display.get("rank"),
                "target_share_pct": safe_float(target_display.get("share_pct")),
                "brand_value_recent": safe_float(recent.get("raw_value")),
                "brand_share_pct": safe_float(target_display.get("share_pct")),
            },
            "sources_data": {
                **latest_market_series_payload(market_series),
                "periods_unit": "월간" if brand_row["source"] == "ubist" else "분기",
                "hhi_series_5y": hhi_points,
                "hhi_recent": hhi_recent,
                "cagr_5y_pct": series_cagr(market_series),
            },
            "market_size_series": market_size_series_with_yoy(market_series),
            "hhi_series_5y": hhi_series,
            "hhi_recent": hhi_recent,
            "brand_ranking": brand_ranking_stacked,
            "company_ranking": company_ranking_stacked,
            "ei_ms_matrix": _matrix_payload(display_entries_no_others),
            "growth_contribution_ms_matrix": _matrix_payload(display_entries_no_others),
            "growth_contribution": growth_contribution,
            "level_top5_trend": level_top5_trend,
            "target_customer_competition": target_customer_competition,
            "brand_ranking_stacked": brand_ranking_stacked,
            "company_ranking_stacked": company_ranking_stacked,
            "company_concentration_trend": company_concentration,
            "data_period_coverage": data_period_coverage,
            "analysis_levels": analysis_levels,
        },
        "market_meta": {
            "strategic_market_id": market_id,
            "market_name": market_name,
            "market_name_short": (BRAND_METADATA_BY_NAME.get(brand_row["brand_name"]).market_name_short if BRAND_METADATA_BY_NAME.get(brand_row["brand_name"]) else market_name),
            "market_label_kor": (BRAND_METADATA_BY_NAME.get(brand_row["brand_name"]).market_label_kor if BRAND_METADATA_BY_NAME.get(brand_row["brand_name"]) else None),
            "market_definition_label": (BRAND_METADATA_BY_NAME.get(brand_row["brand_name"]).market_label_kor if BRAND_METADATA_BY_NAME.get(brand_row["brand_name"]) else market_name),
            "market_definition_full": f"{market_name} 시장 정의" if market_name else None,
            "mkt_team": (BRAND_METADATA_BY_NAME.get(brand_row["brand_name"]).mkt_team if BRAND_METADATA_BY_NAME.get(brand_row["brand_name"]) else None),
            "brand_list": [
                member["name"]
                for member in catalog_members
                if member.get("name") and member.get("is_jw")
            ],
            "atc_codes": (BRAND_METADATA_BY_NAME.get(brand_row["brand_name"]).atc_codes if BRAND_METADATA_BY_NAME.get(brand_row["brand_name"]) else []),
            "atc_desc": (BRAND_METADATA_BY_NAME.get(brand_row["brand_name"]).atc_desc if BRAND_METADATA_BY_NAME.get(brand_row["brand_name"]) else None),
            "view_source_id": view_source_id,
            "atc_count": None,
            "nhi_type": None,
            "sources": market_sources,
            "source_label": source,
            "is_dual_source": len(market_sources) == 2,
            "measures": list(MEASURES_BY_SOURCE.get(brand_row["source"], ())),
            "measures_label": _measure_labels(source),
            "available_levels": analysis_levels.get("levels") or [],
            "direct_competition_count": direct_competition_count,
            "market_size_recent": series_latest_number(market_series),
            "market_cagr_5y_pct": series_cagr(market_series),
            "is_jw": bool(brand_row.get("is_jw")),
            "is_target": bool(brand_row.get("is_target")),
        },
    }


def make_sibling_map(rows: list[dict[str, Any]], market_key: str) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row[market_key], row["source"], row["measure"])].append(row)
    return grouped


def main() -> None:
    args = parser(__doc__).parse_args()
    strategic_brand = load_catalog("strategic_brand")
    ml_market = load_catalog("ml_market").set_index("ml_id", drop=False)
    cd_market = load_catalog("cd_market").rename(columns={"cd_id": "cd_market_id"}).set_index("cd_market_id", drop=False)

    ml_market_rows = {
        (r["ml_id"], r["source"], r["measure"]): r for r in fetch_all("SELECT * FROM mart_strategic_ml_market_metric")
    }
    cd_market_rows = {
        (r["cd_market_id"], r["source"], r["measure"]): r for r in fetch_all("SELECT * FROM mart_strategic_cd_market_metric")
    }
    ml_brand_rows = fetch_all("SELECT * FROM mart_strategic_ml_brand_metric")
    cd_brand_rows = fetch_all("SELECT * FROM mart_strategic_cd_brand_metric")
    ml_siblings = make_sibling_map(ml_brand_rows, "ml_id")
    cd_siblings = make_sibling_map(cd_brand_rows, "cd_market_id")

    columns = ["brand", "view_type", "source", "measure", "market_id", "response_json", "payload_size"]
    placeholders = ", ".join(["%s"] * len(columns))
    names = ", ".join(f"`{c}`" for c in columns)
    sql = f"REPLACE INTO `cache_cause` ({names}) VALUES ({placeholders})"
    inserted = 0
    conn = mariadb_connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM `cache_cause`")
    batch: list[tuple[Any, ...]] = []

    def flush_batch() -> None:
        nonlocal batch
        if not batch:
            return
        cur.executemany(sql, batch)
        batch = []

    for row in ml_brand_rows:
        market = ml_market.loc[row["ml_id"]].to_dict() if row["ml_id"] in ml_market.index else {}
        market_id = ml_to_strategy(row["ml_id"])
        source = api_source(row["source"])
        response = build_response(
            brand_row=row,
            market_row=ml_market_rows.get((row["ml_id"], row["source"], row["measure"]), {}),
            sibling_rows=ml_siblings[(row["ml_id"], row["source"], row["measure"])],
            view_type="market_landscape",
            market_id=market_id,
            source=source,
            measure=row["measure"],
            view_source_id=row["ml_id"],
            market_name=market.get("name"),
            market_sources=source_list(market.get("data_source")),
            market_catalog_row=market,
            strategic_brand=strategic_brand,
        )
        response_json = dump_payload(response)
        out = {
            "brand": row["brand_name"],
            "view_type": "market_landscape",
            "source": source,
            "measure": row["measure"],
            "market_id": market_id,
            "response_json": response_json,
            "payload_size": len(response_json.encode("utf-8")),
        }
        batch.append(tuple(out[col] for col in columns))
        inserted += 1
        if len(batch) >= 20:
            flush_batch()
        if args.verbose and inserted % 1000 == 0:
            print(f"inserted cache_cause rows={inserted}", flush=True)

    for row in cd_brand_rows:
        cd = cd_market.loc[row["cd_market_id"]].to_dict() if row["cd_market_id"] in cd_market.index else {}
        ml_id = cd.get("ml_id") or row.get("ml_id")
        ml = ml_market.loc[ml_id].to_dict() if ml_id in ml_market.index else {}
        market_id = ml_to_strategy(ml_id)
        source = api_source(row["source"])
        response = build_response(
            brand_row=row,
            market_row=cd_market_rows.get((row["cd_market_id"], row["source"], row["measure"]), {}),
            sibling_rows=cd_siblings[(row["cd_market_id"], row["source"], row["measure"])],
            view_type="competitive_dynamics",
            market_id=market_id,
            source=source,
            measure=row["measure"],
            view_source_id=row["cd_market_id"],
            market_name=cd.get("name") or ml.get("name"),
            market_sources=source_list(cd.get("data_source") or ml.get("data_source")),
            market_catalog_row=ml,
            strategic_brand=strategic_brand,
        )
        response_json = dump_payload(response)
        out = {
            "brand": row["brand_name"],
            "view_type": "competitive_dynamics",
            "source": source,
            "measure": row["measure"],
            "market_id": market_id,
            "response_json": response_json,
            "payload_size": len(response_json.encode("utf-8")),
        }
        batch.append(tuple(out[col] for col in columns))
        inserted += 1
        if len(batch) >= 20:
            flush_batch()
        if args.verbose and inserted % 1000 == 0:
            print(f"inserted cache_cause rows={inserted}", flush=True)
    flush_batch()
    cur.close()
    conn.close()
    if args.verbose:
        print(f"cache_cause rows={inserted} ml_rows={len(ml_brand_rows)} cd_rows={len(cd_brand_rows)}")


if __name__ == "__main__":
    main()
