#!/usr/bin/env python3
"""Build spec-aligned cache_cause from Phase 1 strategic marts."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any

from cache_build_common import (
    MEASURES_BY_SOURCE,
    api_source,
    decode_json,
    dump_payload,
    fetch_all,
    load_catalog,
    metric_recent,
    ml_to_strategy,
    mariadb_connect,
    parser,
    payload_size,
    period_key,
    safe_float,
    series_cagr,
    series_latest_number,
    source_list,
)


CHANNELS_5 = ["전체", "상급종병", "종병", "병원", "의원/보건소"]
LEVEL_FIELD_BY_LABEL = {
    "Class": "class",
    "Class 1": "class",
    "Class 2": "class_2",
    "Molecule": "molecule",
    "Brand": "__brand__",
    "제형/투여경로": "dosage_form",
    "용량": "strength_pack",
    "비/급여": "nhi_type",
    "Ox/Gx": "ox_gx",
    "fish_oil": "fish_oil",
}
ANALYSIS_LEVELS_CACHE: dict[tuple[str | None, str, str], dict[str, Any]] = {}


def _period_year(period: str) -> int | None:
    try:
        return int(str(period)[:4])
    except (TypeError, ValueError):
        return None


def _row_value(row: dict[str, Any]) -> float:
    return safe_float(row.get("raw_value") or row.get("value")) or 0.0


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


def _latest_history_item(row: dict[str, Any]) -> dict[str, Any]:
    cached = row.get("__latest_history_item")
    if cached is not None:
        return cached
    history = row.get("__metric_history")
    if history is None:
        history = decode_json(row.get("metric_history"))
        row["__metric_history"] = history
    if not isinstance(history, dict) or not history:
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
    top_n: int = 5,
) -> dict[str, Any]:
    by_year: dict[int, tuple[str, list[dict[str, Any]]]] = {}
    for period, rows in sorted((period_map or {}).items()):
        year = _period_year(period)
        if year is None or not isinstance(rows, list):
            continue
        by_year[year] = (str(period), rows)

    years = sorted(by_year.keys())[-5:]
    yearly = []
    for year in years:
        _, rows = by_year[year]
        normalized = [_normalize_rank_row(row, label_key=label_key, target_name=target_name) for row in rows]
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

        target = next((row for row in normalized if row["is_target"]), None)
        target_id = row_identity(target, label_key)
        competitors = []
        for candidate in sorted(normalized, key=lambda item: item["value"], reverse=True):
            if row_identity(candidate, label_key) != target_id:
                competitors.append(candidate)
        selected = ([target] if target else []) + competitors[:top_n]
        selected_ids = {row_identity(row, label_key) for row in selected}
        others = [row for row in normalized if row_identity(row, label_key) not in selected_ids]
        if others:
            selected.append(
                {
                    label_key: "기타",
                    "brand": "기타" if label_key == "brand" else None,
                    "company": "기타" if label_key == "company" else None,
                    "is_target": False,
                    "is_jw": False,
                    "is_others": True,
                    "value": sum(row["value"] for row in others),
                    "rank": None,
                    "ms_pct": sum(row["ms_pct"] for row in others),
                }
            )
        for index, row in enumerate(selected, start=1):
            row["rank"] = row["rank"] or index
        yearly.append({"year": year, "rankings": selected})
    return {"years": years, "yearly": yearly}


def row_identity(row: dict[str, Any] | None, label_key: str) -> str | None:
    if not row:
        return None
    return str(row.get(label_key) or row.get("brand") or row.get("company") or row.get("name"))


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
    return {
        "levels": levels,
        "channels": ["전체"] if levels else [],
        "period_unit": "monthly" if source == "UBIST" else "quarterly",
        "periods_monthly": [],
        "periods_quarterly": [],
        "data": data,
    }


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
    return normalized


def _history_periods(rows: list[dict[str, Any]], source: str) -> list[str]:
    periods: set[str] = set()
    for row in rows:
        history = row.get("__metric_history")
        if history is None:
            history = decode_json(row.get("metric_history"))
            row["__metric_history"] = history
        if isinstance(history, dict):
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
    levels.append("Brand")
    if bool(market.get("analyze_dosage_form")):
        levels.append("제형/투여경로")
    if bool(market.get("analyze_strength_pack")):
        levels.append("용량")
    if bool(market.get("analyze_nhi_type")):
        levels.append("비/급여")
    if bool(market.get("analyze_ox_gx")):
        levels.append("Ox/Gx")
    if bool(market.get("analyze_fish_oil")):
        levels.append("fish_oil")
    return levels


def _strategic_levels(market: dict[str, Any] | None, view_source_id: str | None) -> list[str]:
    levels = _market_levels(market)
    if view_source_id == "ml_011" and "Class" in levels:
        index = levels.index("Class")
        levels[index : index + 1] = ["Class 1", "Class 2"]
    return levels


def _dimension_value(row: dict[str, Any], level: str) -> str | None:
    if level == "Brand":
        return row.get("brand_name") or row.get("brand_key")
    by_dimension = row.get("__by_dimension")
    if by_dimension is None:
        by_dimension = decode_json(row.get("by_dimension"))
        row["__by_dimension"] = by_dimension
    if not isinstance(by_dimension, dict):
        by_dimension = {}
    field = LEVEL_FIELD_BY_LABEL.get(level)
    candidates = [field] if field else []
    if level == "Class 2":
        candidates.extend(["class2", "class_2", "class_secondary", "class_sub", "class"])
    if level == "Class 1":
        candidates.extend(["class1", "class_1", "class_primary", "class"])
    for candidate in candidates:
        if not candidate:
            continue
        value = by_dimension.get(candidate)
        if value not in (None, "", [], {}):
            return str(value)
    return None


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
    if text.upper() == "KHPA":
        return "병원"
    if text.upper() == "KPA":
        return "의원/보건소"
    return None


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
    top_n: int = 5,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, list[float]]] = {}
    totals: dict[str, list[float]] = {period: [0.0] for period in periods}

    for row in rows:
        name = _dimension_value(row, level)
        if not name:
            continue
        grouped.setdefault(name, {period: [0.0] for period in periods})
        if channel == "전체":
            history = row.get("__metric_history")
            if history is None:
                history = decode_json(row.get("metric_history"))
                row["__metric_history"] = history
            if isinstance(history, dict):
                _add_series(grouped[name], history, periods)
                _add_series(totals, history, periods)
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
                _add_series(grouped[name], series, periods)
                _add_series(totals, series, periods)

    ranked = sorted(
        grouped.items(),
        key=lambda item: item[1][periods[-1]][0] if periods else 0.0,
        reverse=True,
    )
    if target_name:
        ranked = sorted(ranked, key=lambda item: (item[0] != target_name, -(item[1][periods[-1]][0] if periods else 0.0)))
    selected = ranked[:top_n]

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


def _build_analysis_levels_from_mart(
    *,
    rows: list[dict[str, Any]],
    source: str,
    market: dict[str, Any] | None,
    view_source_id: str | None,
    target_name: str | None,
    fallback_level_top5: dict[str, Any],
) -> dict[str, Any]:
    levels = _strategic_levels(market, view_source_id)
    if not levels:
        return _normalize_analysis_levels({}, fallback_level_top5, source)
    periods = _history_periods(rows, source)
    data: dict[str, Any] = {}
    for level in levels:
        by_channel = {
            channel: _segment_rows_for_level(
                rows=rows,
                level=level,
                periods=periods,
                source=source,
                channel=channel,
                target_name=target_name if level == "Brand" else None,
            )
            for channel in CHANNELS_5
        }
        data[level] = {"segments": by_channel["전체"], "by_channel": by_channel}
    return {
        "levels": levels,
        "channels": CHANNELS_5,
        "period_unit": _period_unit_ko(source),
        "periods_monthly": periods if source == "UBIST" else [],
        "periods_quarterly": periods if source == "IQVIA" else [],
        "data": data,
    }


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
    history = row.get("__metric_history")
    if history is None:
        history = decode_json(row.get("metric_history"))
        row["__metric_history"] = history
    if not isinstance(history, dict):
        history = {}
    scale = 100_000_000 if scaled_sales and row.get("measure") == "sales" else 1
    values = []
    for period in periods:
        value = _value_from_period_item(history.get(period))
        values.append(round(value / scale, 4))
    series_cache[cache_key] = values
    return values


def _display_brand_rows(
    rows: list[dict[str, Any]],
    *,
    target_name: str | None,
    top_n: int = 5,
    include_others: bool,
) -> list[dict[str, Any]]:
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
                "value_recent": value_recent,
                "raw_value": value_recent,
                "share_pct": share,
                "ms_pct": share,
                "ms_recent_pct": share,
                "ei": safe_float(extended.get("ei_5y") or extended.get("ei")) or 0.0,
                "ei_5y": safe_float(extended.get("ei_5y") or extended.get("ei")) or 0.0,
                "cagr_5y_pct": (safe_float(extended.get("cagr_5y")) or 0.0) * 100,
                "momentum_score": safe_float(extended.get("momentum_score")) or 0.0,
                "growth_contribution": growth_contribution,
                "growth_contribution_pct": growth_contribution,
                "contribution": growth_contribution,
                "contribution_pct": growth_contribution,
                "_source_row": row,
            }
        )

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
        selected.append(
            {
                "brand": "기타",
                "brand_key": "기타",
                "company": f"{len(others)}개 brand",
                "is_target": False,
                "is_jw": False,
                "is_others": True,
                "rank": None,
                "value_recent": sum(row["value_recent"] for row in others),
                "raw_value": sum(row["raw_value"] for row in others),
                "share_pct": sum(row["share_pct"] for row in others),
                "ms_pct": sum(row["ms_pct"] for row in others),
                "ms_recent_pct": sum(row["ms_recent_pct"] for row in others),
                "ei": 0.0,
                "ei_5y": 0.0,
                "cagr_5y_pct": 0.0,
                "momentum_score": 0.0,
                "growth_contribution": sum(row["growth_contribution"] for row in others),
                "growth_contribution_pct": sum(row["growth_contribution_pct"] for row in others),
                "contribution": sum(row["contribution"] for row in others),
                "contribution_pct": sum(row["contribution_pct"] for row in others),
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
        points.append({"period": period, "year": year, value_key: value or 0.0})
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


def _growth_contribution_payload(entries_with_others: list[dict[str, Any]], periods: list[str]) -> dict[str, Any]:
    by_brand = {
        "top_contributors": [
            {
                "brand": row.get("brand"),
                "company": row.get("company"),
                "is_target": bool(row.get("is_target")),
                "is_jw": bool(row.get("is_jw")),
                "is_others": bool(row.get("is_others")),
                "contribution": safe_float(row.get("growth_contribution")) or 0.0,
                "contribution_pct": safe_float(row.get("growth_contribution_pct")) or 0.0,
                "value_recent": safe_float(row.get("value_recent")) or 0.0,
            }
            for row in entries_with_others
        ],
        "others_total": 0.0,
    }
    target_company = next((row.get("company") for row in entries_with_others if row.get("is_target")), None)
    market_growth = sum(row["contribution"] for row in by_brand["top_contributors"])
    return {
        "period_start": periods[0] if periods else None,
        "period_end": periods[-1] if periods else None,
        "market_start": None,
        "market_end": None,
        "market_growth": market_growth,
        "by_brand": by_brand,
        "by_company": _company_waterfall(entries_with_others, target_company=target_company),
    }


def _target_customer_competition(
    *,
    rows: list[dict[str, Any]],
    source: str,
    target_name: str | None,
    periods: list[str],
) -> dict[str, Any]:
    targets = ["전체", "상급종병", "종병", "병원", "의원/보건소"] if source == "UBIST" else ["전체", "KHPA", "KCPA", "KPA"]
    target_type = "진료과" if source == "UBIST" else "채널"
    period_tail = periods[-10:]
    row_by_brand = {_row_brand(row): row for row in rows if _row_brand(row)}
    views = []
    for target in targets:
        selected = _display_brand_rows(rows, target_name=target_name, top_n=5, include_others=True)
        trend_brands = []
        composition = []
        for item in selected:
            source_row = row_by_brand.get(item.get("brand"))
            value_series = _series_for_row(source_row or {}, period_tail, scaled_sales=True) if source_row else [0.0] * len(period_tail)
            trend_brands.append(
                {
                    "brand": item.get("brand"),
                    "company": item.get("company"),
                    "is_target": item.get("is_target"),
                    "is_jw": item.get("is_jw"),
                    "is_others": item.get("is_others"),
                    "value_series": value_series,
                    "volume_series": value_series,
                }
            )
            composition.append(
                {
                    "brand": item.get("brand"),
                    "is_target": item.get("is_target"),
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
            }
        )
    return {
        "available_in_view": ["market_landscape", "competitive_dynamics"],
        "target_type": target_type,
        "targets": targets,
        "note": f"{source} {target_type} 기준 top 5 + 기타",
        "views": views,
    }


def _level_top5_trend(analysis_levels: dict[str, Any], rows: list[dict[str, Any]], source: str, target_name: str | None) -> dict[str, Any]:
    levels = analysis_levels.get("levels") or []
    periods = (analysis_levels.get("periods_monthly") or analysis_levels.get("periods_quarterly") or [])[-10:]
    available_levels = [{"key": level, "label": level} for level in levels]
    by_level = {}
    brand_entries = _display_brand_rows(rows, target_name=target_name, top_n=5, include_others=True)
    row_by_brand = {_row_brand(row): row for row in rows if _row_brand(row)}
    for level in levels:
        level_segments = (analysis_levels.get("data", {}).get(level, {}).get("by_channel", {}).get("전체") or [])[:5]
        values = []
        for index, segment in enumerate(level_segments, start=1):
            brands_in_value = []
            for entry in brand_entries:
                source_row = row_by_brand.get(entry.get("brand"))
                series = _series_for_row(source_row or {}, periods, scaled_sales=True) if source_row else [0.0] * len(periods)
                brands_in_value.append(
                    {
                        "brand": entry.get("brand"),
                        "is_target": entry.get("is_target"),
                        "is_jw": entry.get("is_jw"),
                        "is_others": entry.get("is_others"),
                        "ms_recent_pct": safe_float(entry.get("share_pct")) or 0.0,
                        "value_recent_100m": round((safe_float(entry.get("value_recent")) or 0.0) / 100_000_000, 4),
                        "volume_recent": safe_float(entry.get("value_recent")) or 0.0,
                        "value_series_10pt": series,
                        "volume_series_10pt": series,
                    }
                )
            total_value = sum(item.get("value_recent_100m", 0.0) for item in brands_in_value) * 100_000_000
            values.append(
                {
                    "value": segment.get("name") or f"{level} {index}",
                    "is_default": index == 1,
                    "total_value": total_value,
                    "total_volume": total_value,
                    "ms_pct": safe_float(segment.get("recent_share_pct")) or 0.0,
                    "brands_in_value": brands_in_value,
                }
            )
        by_level[level] = {
            "level_label": level,
            "periods_10pt": periods,
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


def latest_market_series_payload(series: dict[str, Any]) -> dict[str, Any]:
    return {
        "periods_unit": "월간",
        "periods_count": len(series or {}),
        "market_size_series": series or {},
    }


def top3_share(rows: list[dict[str, Any]]) -> float | None:
    shares = []
    for row in rows:
        recent = metric_recent(decode_json(row.get("metric_history")))
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
    target_recent = metric_recent(decode_json(target.get("metric_history")))
    target_ext = metric_recent(decode_json(target.get("extended_metric_history")))

    brand_ranking = decode_json(market_row.get("brand_ranking_stacked"))
    company_ranking = decode_json(market_row.get("company_ranking_stacked"))
    level_top5 = decode_json(market_row.get("level_top5_trend"))
    catalog_members = _catalog_members_for_market(strategic_brand, view_source_id)
    analysis_view_id = view_source_id if str(view_source_id).startswith("ml_") else market_catalog_row.get("ml_id") if market_catalog_row else None
    analysis_cache_key = (analysis_view_id, source_api, measure)
    if analysis_cache_key not in ANALYSIS_LEVELS_CACHE:
        ANALYSIS_LEVELS_CACHE[analysis_cache_key] = _build_analysis_levels_from_mart(
            rows=sibling_rows,
            source=source_api,
            market=market_catalog_row,
            view_source_id=analysis_view_id,
            target_name=None,
            fallback_level_top5=level_top5,
        )
    analysis_levels = deepcopy(ANALYSIS_LEVELS_CACHE[analysis_cache_key])
    brand_ranking_stacked = _stacked_ranking(
        brand_ranking,
        label_key="brand",
        target_name=brand_row.get("brand_name"),
        catalog_members=catalog_members,
    )
    company_ranking_stacked = _stacked_ranking(company_ranking, label_key="company", target_name=target.get("company_name"))
    display_entries_no_others = _display_brand_rows(
        sibling_rows,
        target_name=brand_row.get("brand_name"),
        top_n=5,
        include_others=False,
    )
    display_entries_with_others = _display_brand_rows(
        sibling_rows,
        target_name=brand_row.get("brand_name"),
        top_n=5,
        include_others=True,
    )
    periods = _history_periods(sibling_rows, source_api)
    hhi_points = _annual_latest_points(hhi_series, value_key="hhi")
    company_concentration = _company_hhi_from_ranking(company_ranking)
    growth_contribution = _growth_contribution_payload(display_entries_with_others, periods)
    target_customer_competition = _target_customer_competition(
        rows=sibling_rows,
        source=source_api,
        target_name=brand_row.get("brand_name"),
        periods=periods,
    )
    level_top5_trend = _level_top5_trend(
        analysis_levels,
        sibling_rows,
        source_api,
        brand_row.get("brand_name"),
    )
    direct_competition_count = max(
        len({r.get("brand_key") for r in sibling_rows if r.get("brand_key")}),
        len({member["name"] for member in catalog_members if member.get("name")}),
    )

    return {
        "brand": brand_row["brand_name"],
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
                "target_company": target.get("company_name") or ("JW중외제약" if target.get("is_jw") else None),
                "target_ei": safe_float(target_ext.get("ei")),
                "target_momentum": safe_float(target_ext.get("momentum") or target_recent.get("mom")),
                "target_rank": target_recent.get("rank"),
                "target_share_pct": safe_float(target_recent.get("ms")),
                "brand_value_recent": safe_float(recent.get("raw_value")),
                "brand_share_pct": safe_float(recent.get("ms")),
            },
            "sources_data": {
                **latest_market_series_payload(market_series),
                "periods_unit": "월간" if brand_row["source"] == "ubist" else "분기",
                "hhi_series_5y": hhi_points,
                "hhi_recent": hhi_recent,
                "cagr_5y_pct": series_cagr(market_series),
            },
            "ei_ms_matrix": _matrix_payload(display_entries_no_others),
            "growth_contribution_ms_matrix": _matrix_payload(display_entries_no_others),
            "growth_contribution": growth_contribution,
            "level_top5_trend": level_top5_trend,
            "target_customer_competition": target_customer_competition,
            "brand_ranking_stacked": brand_ranking_stacked,
            "company_ranking_stacked": company_ranking_stacked,
            "company_concentration_trend": company_concentration,
            "analysis_levels": analysis_levels,
        },
        "market_meta": {
            "market_name": market_name,
            "view_source_id": view_source_id,
            "atc_count": None,
            "nhi_type": None,
            "sources": market_sources,
            "source_label": source,
            "is_dual_source": len(market_sources) == 2,
            "measures": list(MEASURES_BY_SOURCE.get(brand_row["source"], ())),
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
        out = {
            "brand": row["brand_name"],
            "view_type": "market_landscape",
            "source": source,
            "measure": row["measure"],
            "market_id": market_id,
            "response_json": dump_payload(response),
            "payload_size": payload_size(response),
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
        out = {
            "brand": row["brand_name"],
            "view_type": "competitive_dynamics",
            "source": source,
            "measure": row["measure"],
            "market_id": market_id,
            "response_json": dump_payload(response),
            "payload_size": payload_size(response),
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
