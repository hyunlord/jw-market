#!/usr/bin/env python3
"""Build spec-aligned cache_cause from Phase 1 strategic marts.

원인분석 payload 계약:
- brand/company ranking은 선택 대상과 경쟁 상위 5가 차지한 실제 연간
  순위까지 모든 중간 순위를 포함한다. 선택 대상이 top5 밖이거나 값이
  0이어도 명시적으로 포함한다.
- level_top5_trend의 "전체" 옵션은 전체 시장 기준이므로 선택 브랜드를
  포함한다. 반대로 개별 segment 옵션은 그 분류 안의 top5+기타만 보여주며
  선택 브랜드를 강제로 끼우지 않는다.
- M/S 폴라의 100% overall slice는 제거하되, 매출 추이의 전체 line은 남긴다.
  전체 옵션 자체를 삭제하는 대안은 운영 chart8의 전체 시장 기준 뷰를 깨서
  기각했다.
"""

from __future__ import annotations

from array import array
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
import logging
import os
from pathlib import Path
import re
import sys
from time import perf_counter
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cache_build_common import (
    MEASURES_BY_SOURCE,
    active_catalog_member_rows,
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
from pipeline.scripts.api.dynamic_market.cause_sections import matrix_growth_value
from pipeline.scripts.api.dynamic_market.cause_ranking import selected_annual_rank_prefix
from pipeline.scripts.api.dynamic_market.analysis_level_block_replay import (
    AnalysisLevelBlockKey,
    current_analysis_level_source_epoch,
    load_analysis_level_block,
)
from pipeline.scripts.api.market_growth import fixed_five_year_growth_series
from pipeline.scripts.api.metadata.ml_market_meta import BRAND_METADATA
from pipeline.scripts.etl.iron_iv_dimensions import FE_CONTENT_FIELD, FE_CONTENT_LEVEL, is_iron_iv_dimension_market
from pipeline.scripts.etl.ubist_channel_resolver import resolve_market_channels, strategic_channel_totals_context

period_key = lru_cache(maxsize=None)(period_key)

logger = logging.getLogger(__name__)


def _latency_stage_timing_enabled() -> bool:
    return os.getenv("LATENCY_STAGE_TIMING", "").strip().lower() in {"1", "true", "yes", "on"}


UBIST_TGH_FACILITY_CHANNEL = "(상급종병 + 종병)"
UBIST_TGH_FACILITY_BUCKETS = {"상급종병", "종병"}
CHANNELS_5 = ["전체", "상급종병", "종병", UBIST_TGH_FACILITY_CHANNEL, "병원", "의원", "보건소", "기타"]
IQVIA_CHANNELS = ["전체", "KHPA", "KCPA", "KPA"]
CAUSE_LEVELS_V091 = ["Class", "Molecule", "Brand", "제형/투여경로", "용량", "비/급여", "Ox/Gx"]
FISH_OIL_LEVEL = "Fish Oil"
UNCLASSIFIED_DIMENSION_NAME = "미분류"
LEVEL_FIELD_BY_LABEL = {
    "Class": "class",
    "Class 1": "class_1",
    "Class 2": "class_2",
    "Molecule": "molecule",
    "제형/투여경로": "dosage_form",
    "용량": "strength_pack",
    "비/급여": "nhi_type",
    "Ox/Gx": "ox_gx",
    FE_CONTENT_LEVEL: FE_CONTENT_FIELD,
    FISH_OIL_LEVEL: "fish_oil",
    "fish_oil": "fish_oil",
}
SPECIALTY_DIMENSION_FIELDS = {"molecule", "dosage_form", "strength_pack", "nhi_type", FE_CONTENT_FIELD}
ANALYSIS_LEVELS_CACHE: dict[tuple[str | None, str, str], dict[str, Any]] = {}
LEVEL_ROW_GROUPS_CACHE: dict[tuple[str | None, str, str], dict[str, dict[str, list[dict[str, Any]]]]] = {}
ANALYSIS_LEVELS_BY_CHANNEL_CACHE: dict[Any, dict[str, Any]] = {}
ANALYSIS_LEVEL_STATUS_CHANNEL_CACHE: dict[Any, dict[str, Any]] = {}
EI_META_CACHE: dict[tuple[Any, Any], dict[str, Any]] = {}
TARGET_RANK_STATS_CACHE: dict[Any, dict[int, dict[str, dict[str, Any]]]] = {}
BRAND_METADATA_BY_NAME = {item.brand: item for item in BRAND_METADATA}

_SeriesValueCache = dict[
    tuple[int, tuple[str, ...]],
    tuple[dict[str, Any], array],
]
_SeriesObservedCache = dict[
    tuple[int, tuple[str, ...]],
    tuple[dict[str, Any], array, tuple[bool, ...]],
]
_AnnualRankRows = tuple[dict[int, list[dict[str, Any]]], dict[int, int]]
_AnnualRankRowsCache = dict[tuple[int, str], _AnnualRankRows]


def parse_args() -> Any:
    cli = parser(__doc__)
    cli.add_argument(
        "--market",
        default=None,
        help="Regenerate only one ML market and its CD views, for example ml_006.",
    )
    cli.add_argument(
        "--target-table",
        default="cache_cause",
        help="Destination table for cache_cause rows. Used for local blue-green staging.",
    )
    cli.add_argument(
        "--full-all-brands",
        action="store_true",
        help="Debug mode: write every mart brand row. Default serving cache writes only cache_brands canonical brands.",
    )
    return cli.parse_args()


def _quoted_table_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", str(name or "")):
        raise SystemExit(f"unsafe --target-table: {name!r}")
    return f"`{name}`"


def prepare_full_target_table(cur: Any, requested_table: str) -> tuple[str, bool]:
    """Prepare a full-build target table without deleting live cache_cause first."""
    if requested_table == "cache_cause":
        target = "cache_cause_staging"
        should_switch = True
    else:
        target = requested_table
        should_switch = False
    quoted = _quoted_table_name(target)
    cur.execute(f"DROP TABLE IF EXISTS {quoted}")
    cur.execute(f"CREATE TABLE {quoted} LIKE `cache_cause`")
    return target, should_switch


def switch_full_cache_cause(cur: Any, staging_table: str, *, timestamp: str | None = None) -> str:
    ts = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    old_table = f"cache_cause_old_fullregen_{ts}"
    cur.execute(
        f"RENAME TABLE `cache_cause` TO {_quoted_table_name(old_table)}, "
        f"{_quoted_table_name(staging_table)} TO `cache_cause`"
    )
    return old_table


def serving_brand_names_from_cache_brands_payload(payload: Any) -> set[str]:
    if isinstance(payload, str):
        payload = decode_json(payload)
    if isinstance(payload, dict):
        items = payload.get("brands") or payload.get("data") or payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    names: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("brand") or item.get("brand_name") or item.get("name")
        if value:
            names.add(str(value))
    return names


def load_serving_brand_names(cur: Any) -> set[str]:
    cur.execute("SELECT response_json FROM cache_brands WHERE query_key='default' LIMIT 1")
    row = cur.fetchone()
    if not row:
        raise SystemExit("cache_brands default row is required for serving-slim cache_cause build")
    response_json = row.get("response_json") if isinstance(row, dict) else row[0]
    names = serving_brand_names_from_cache_brands_payload(response_json)
    if not names:
        raise SystemExit("cache_brands default row did not contain serving brand names")
    metadata_names = {item.brand for item in BRAND_METADATA}
    if names != metadata_names:
        raise SystemExit(
            f"cache_brands mismatch with canonical metadata: missing={sorted(metadata_names - names)}, "
            f"extra={sorted(names - metadata_names)}"
        )
    return names


def filter_serving_brand_rows(
    rows: list[dict[str, Any]],
    serving_brand_names: set[str],
    *,
    full_all_brands: bool = False,
) -> list[dict[str, Any]]:
    if full_all_brands:
        return rows
    # cache_cause는 배포 포탈 전용 serving cache다. 시장 계산은 sibling_rows
    # 전체를 계속 쓰고, 출력 row만 /api/brands와 같은 canonical 25로 제한한다.
    # full 3,450행은 19GB급 dead weight라 Wave 3a 빌드를 멈추게 했고,
    # 별도 full table을 두는 대안은 프론트 전용 계약 확인으로 기각했다.
    return [row for row in rows if str(row.get("brand_name") or "") in serving_brand_names]


def selected_query(table: str, column: str, values: list[str] | None) -> tuple[str, list[str]]:
    if values is None:
        return f"SELECT * FROM {table}", []
    if not values:
        return f"SELECT * FROM {table} WHERE 1=0", []
    placeholders = ",".join(["%s"] * len(values))
    return f"SELECT * FROM {table} WHERE {column} IN ({placeholders})", values


def _period_year(period: str) -> int | None:
    try:
        return int(str(period)[:4])
    except (TypeError, ValueError):
        return None


def _first_optional_float(*values: Any) -> float | None:
    for value in values:
        parsed = safe_float(value)
        if parsed is not None:
            return parsed
    return None


def _optional_row_value(row: dict[str, Any]) -> float | None:
    parsed = safe_float(row.get("raw_value"))
    if parsed is not None:
        return parsed
    parsed = safe_float(row.get("value"))
    if parsed is not None:
        return parsed
    return safe_float(row.get("sales"))


def _optional_row_share(row: dict[str, Any]) -> float | None:
    return _first_optional_float(row.get("ms"), row.get("ms_pct"), row.get("share_pct"))


def _sum_optional_complete(values: Iterable[Any]) -> float | None:
    parsed = [safe_float(value) for value in values]
    if any(value is None for value in parsed):
        return None
    return sum(value for value in parsed if value is not None)


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
        "value": _optional_row_value(row),
        "rank": row.get("rank"),
        "ms_pct": _optional_row_share(row),
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
    annual_rank_cache: _AnnualRankRowsCache | None = None,
) -> dict[str, Any]:
    by_year, period_count_by_year = _annual_rank_rows(
        period_map,
        label_key=label_key,
        target_name=target_name,
        full_rows=full_rows,
        annual_rank_cache=annual_rank_cache,
    )

    years = sorted(by_year.keys())[-5:]
    yearly = []
    normalized_by_year: dict[int, list[dict[str, Any]]] = {}

    latest_rows = [row for row in by_year.get(years[-1], []) if row_identity(row, label_key)] if years else []
    latest_ranked = sorted(
        latest_rows,
        key=lambda item: (
            safe_float(item.get("value")) is not None,
            safe_float(item.get("value")) or 0.0,
        ),
        reverse=True,
    )
    target = next((row for row in latest_ranked if target_name and row_identity(row, label_key) == target_name), None)
    target_id = row_identity(target, label_key)
    competitors = [row for row in latest_ranked if row_identity(row, label_key) and row_identity(row, label_key) != target_id]
    visible_candidates = ([target] if target else []) + competitors[:top_n]
    visible_ids = [
        row_identity(row, label_key)
        for row in visible_candidates
        if row_identity(row, label_key)
    ]
    visible_id_set = set(visible_ids)

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
        annual_order = [
            str(row_identity(row, label_key))
            for row in normalized
            if row_identity(row, label_key) and isinstance(row.get("rank"), int)
        ]
        selected_order = selected_annual_rank_prefix(annual_order, visible_id_set)
        selected = [row_by_id[item_id] for item_id in selected_order]
        selected.extend(
            row_by_id.get(item_id)
            or _zero_rank_row(item_id, label_key=label_key, target_name=target_name)
            for item_id in visible_ids
            if item_id not in selected_order
        )
        selected_ids = {row_identity(row, label_key) for row in selected}
        others = [row for row in normalized if row_identity(row, label_key) not in selected_ids]
        displayed_ms = _sum_optional_complete(row.get("ms_pct") for row in selected)
        others_value = _sum_optional_complete(row.get("value") for row in others)
        all_shares_complete = all(safe_float(row.get("ms_pct")) is not None for row in normalized)
        selected.append(
            {
                label_key: "기타",
                "brand": "기타" if label_key == "brand" else None,
                "company": "기타" if label_key == "company" else None,
                "is_target": False,
                "is_jw": False,
                "is_others": True,
                "value": others_value,
                "rank": None,
                "ms_pct": (
                    round(max(0.0, 100.0 - displayed_ms), 4)
                    if displayed_ms is not None and all_shares_complete
                    else None
                ),
            }
        )
        if others_value is None or not all_shares_complete:
            selected[-1]["data_quality"] = {"available": False, "reason": "no_data"}
        yearly.append({"year": year, "rankings": selected})

    trend_key = "brands" if label_key == "brand" else "companies"
    emitted_ids: list[str] = []
    for item in yearly:
        for row in item["rankings"]:
            name = row_identity(row, label_key)
            if name and name not in emitted_ids:
                emitted_ids.append(str(name))
    series = {
        name: [
            safe_float(next((row.get("value") for row in item["rankings"] if row_identity(row, label_key) == name), None))
            for item in yearly
        ]
        for name in emitted_ids
    }
    rankings_by_year = {
        str(year): [
            {
                "rank": row.get("rank"),
                label_key: row.get(label_key),
                "brand": row.get("brand"),
                "company": row.get("company"),
                "value": safe_float(row.get("value")),
                "ms_pct": safe_float(row.get("ms_pct")),
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
        "top_brands": [*visible_ids, "기타"],
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
    annual_rank_cache: _AnnualRankRowsCache | None = None,
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, int]]:
    if full_rows:
        return _annual_rank_rows_from_full_rows(
            full_rows,
            label_key=label_key,
            target_name=target_name,
            annual_rank_cache=annual_rank_cache,
        )
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
            value = safe_float(normalized.get("value"))
            if not name:
                continue
            # 연간 ranking/HHI는 연말 스냅샷이 아니라 full-year 합산이다.
            # 연말 한 달/분기만 쓰면 2024/2025 HHI가 기간 믹스에 민감해져
            # PL이 재검산한 full-year 기준과 어긋난다. latest snapshot 유지
            # 대안은 partial 표시에는 간단하지만 연간 농도 지표 의미가 틀려
            # 기각했다.
            bucket = grouped[year].setdefault(
                name,
                {
                    label_key: name,
                    "brand": normalized.get("brand") if label_key == "brand" else None,
                    "company": normalized.get("company") if label_key == "company" else normalized.get("company"),
                    "is_target": bool(normalized.get("is_target")),
                    "is_jw": bool(normalized.get("is_jw")),
                    "is_others": False,
                    "value": 0.0,
                    "rank": None,
                    "ms_pct": 0.0,
                    "_complete": True,
                },
            )
            if value is None:
                bucket["_complete"] = False
            else:
                bucket["value"] += value
            bucket["is_jw"] = bool(bucket.get("is_jw") or normalized.get("is_jw"))
            bucket["is_target"] = bool(bucket.get("is_target") or normalized.get("is_target"))
    for rows in grouped.values():
        for bucket in rows.values():
            if not bucket.pop("_complete"):
                bucket["value"] = None
    return {year: _rank_normalized_rows(list(rows.values()), label_key=label_key) for year, rows in grouped.items()}, dict(period_count_by_year)


def _annual_rank_rows_from_full_rows(
    rows: list[dict[str, Any]],
    *,
    label_key: str,
    target_name: str | None,
    annual_rank_cache: _AnnualRankRowsCache | None = None,
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, int]]:
    cache_key = (id(rows), label_key)
    cached = annual_rank_cache.get(cache_key) if annual_rank_cache is not None else None
    if cached is None:
        if annual_rank_cache is not None:
            _populate_annual_rank_rows_cache(rows, annual_rank_cache, label_keys=("brand", "company"))
            cached = annual_rank_cache[cache_key]
        else:
            reduced: _AnnualRankRowsCache = {}
            _populate_annual_rank_rows_cache(rows, reduced, label_keys=(label_key,))
            cached = reduced[cache_key]
    by_year, period_count_by_year = cached
    if not target_name:
        return by_year, period_count_by_year

    targeted_by_year = deepcopy(by_year)
    for year_rows in targeted_by_year.values():
        for row in year_rows:
            is_target = row_identity(row, label_key) == target_name
            row["is_target"] = is_target
            row["is_jw"] = bool(row.get("is_jw")) or is_target
    return targeted_by_year, period_count_by_year


def _populate_annual_rank_rows_cache(
    rows: list[dict[str, Any]],
    cache: _AnnualRankRowsCache,
    *,
    label_keys: tuple[str, ...],
) -> None:
    periods_by_label: dict[str, dict[int, set[str]]] = {
        label_key: defaultdict(set) for label_key in label_keys
    }
    grouped_by_label: dict[str, dict[int, dict[str, dict[str, Any]]]] = {
        label_key: defaultdict(dict) for label_key in label_keys
    }
    for row in rows:
        history = _metric_history(row)
        if not history:
            continue
        names = {
            label_key: _row_company(row) if label_key == "company" else _row_brand(row)
            for label_key in label_keys
        }
        if all(not name for name in names.values()):
            continue
        for period, item in history.items():
            year = _period_year(str(period))
            if year is None:
                continue
            period_str = str(period)
            for label_key, name in names.items():
                if not name:
                    continue
                periods_by_label[label_key][year].add(period_str)
                bucket = grouped_by_label[label_key][year].setdefault(
                    name,
                    {
                        label_key: name,
                        "brand": name if label_key == "brand" else None,
                        "company": _row_company(row) if label_key == "brand" else name,
                        "is_target": False,
                        "is_jw": bool(row.get("is_jw")),
                        "is_others": False,
                        "value": 0.0,
                        "rank": None,
                        "ms_pct": 0.0,
                        "_complete": True,
                    },
                )
                value = _optional_value_from_period_item(item)
                if value is None:
                    bucket["_complete"] = False
                else:
                    bucket["value"] += value
                bucket["is_jw"] = bool(bucket.get("is_jw") or row.get("is_jw"))
    for label_key in label_keys:
        grouped = grouped_by_label[label_key]
        periods_by_year = periods_by_label[label_key]
        for items in grouped.values():
            for item in items.values():
                if not item.pop("_complete"):
                    item["value"] = None
        cache[(id(rows), label_key)] = (
            {
                year: _rank_normalized_rows(list(items.values()), label_key=label_key)
                for year, items in grouped.items()
            },
            {year: len(periods) for year, periods in periods_by_year.items()},
        )


def _rank_normalized_rows(rows: list[dict[str, Any]], *, label_key: str) -> list[dict[str, Any]]:
    values = {id(row): safe_float(row.get("value")) for row in rows}
    ranked = sorted(
        rows,
        key=lambda item: (
            values[id(item)] is not None,
            values[id(item)] if values[id(item)] is not None else 0.0,
        ),
        reverse=True,
    )
    total = sum(value for value in values.values() if value is not None)
    for index, row in enumerate(ranked, start=1):
        value = values[id(row)]
        row["rank"] = index if value is not None and value > 0 else None
        row["ms_pct"] = round(value / total * 100, 4) if value is not None and total > 0 else (0.0 if value == 0.0 else None)
        if value is None:
            row["data_quality"] = {"available": False, "reason": "no_data"}
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

    ranked_by_year: dict[int, list[dict[str, Any]]] = {}

    def ranked_rows(year: int) -> list[dict[str, Any]]:
        cached = ranked_by_year.get(year)
        if cached is not None:
            return cached
        values: dict[int, float | None] = {}
        rows = []
        for row in normalized_by_year.get(year, []):
            row_id = identity(row)
            if not row_id or row.get("is_others"):
                continue
            value = safe_float(row.get("value"))
            if (value is not None and value > 0) or bool(target_name and row_id == target_name):
                values[id(row)] = value
                rows.append(row)
        ranked = sorted(
            rows,
            key=lambda item: (
                values[id(item)] is not None,
                values[id(item)] or 0.0,
            ),
            reverse=True,
        )
        for index, row in enumerate(ranked, start=1):
            row.setdefault("rank", index)
        ranked_by_year[year] = ranked
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
                    "value": safe_float(row.get("value")) if row else None,
                    "ms_pct": safe_float(row.get("ms_pct")) if row else None,
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
            displayed_ms = _sum_optional_complete(
                row.get("ms_pct") for row in rows if identity(row) in selected_ids
            )
            others_value = _sum_optional_complete(row.get("value") for row in others)
            all_shares_complete = all(safe_float(row.get("ms_pct")) is not None for row in rows)
            yearly_values.append(
                {
                    "year": year,
                    "value": others_value,
                    "ms_pct": (
                        round(max(0.0, 100.0 - displayed_ms), 4)
                        if displayed_ms is not None and all_shares_complete
                        else None
                    ),
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


def _target_rank_overrides(
    rows: list[dict[str, Any]],
    *,
    label_key: str,
    target_name: str | None,
    cache_key: Any = None,
    annual_rank_cache: _AnnualRankRowsCache | None = None,
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
            annual_rank_cache=annual_rank_cache,
        )
        TARGET_RANK_STATS_CACHE[stats_key] = {
            year: {
                row_identity(row, label_key): {
                    "row": row,
                    "value": safe_float(row.get("value")),
                    "rank": row.get("rank"),
                    "ms_pct": safe_float(row.get("ms_pct")),
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
        latest_values = [_optional_row_value(row) for row in latest]
        total = _sum_optional_complete(latest_values)
        segments = [
            {
                "name": row.get("label") or row.get("level") or row.get("name") or row.get(level),
                "rank": row.get("rank") or idx,
                "recent_share_pct": _first_optional_float(row.get("ms"), row.get("share_pct")),
                "series_pct": [(_optional_row_share(row) if latest_period else None)],
                "value_series": [_optional_row_value(row)],
            }
            for idx, row in enumerate(latest, start=1)
        ]
        for segment in segments:
            if segment["value_series"][-1] is None:
                segment["data_quality"] = {"available": False, "reason": "no_data"}
        if total and not any(segment.get("recent_share_pct") is not None for segment in segments):
            for segment in segments:
                value = segment["value_series"][-1]
                segment["recent_share_pct"] = round((value / total) * 100, 4) if value is not None else None
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


def _series_from_period_map(period_map: dict[str, Any]) -> tuple[list[float | None], list[float | None]]:
    values: list[float | None] = []
    shares: list[float | None] = []
    for _, item in sorted((period_map or {}).items()):
        if isinstance(item, dict):
            values.append(_optional_row_value(item))
            shares.append(_optional_row_share(item))
        else:
            values.append(safe_float(item))
            shares.append(None)
    if values and not any(share is not None for share in shares):
        total = _sum_optional_complete(values)
        shares = [
            round(value / total * 100, 4)
            if value is not None and total is not None and total > 0
            else (0.0 if value == 0.0 and total == 0.0 else None)
            for value in values
        ]
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
                    recent_value = values[-1] if values else None
                    recent_share = shares[-1] if shares else None
                    ranked.append((recent_value, name, values, shares, recent_share))
                ranked.sort(key=lambda item: (item[0] is not None, item[0] or 0.0), reverse=True)
                for idx, (recent_value, name, values, shares, recent_share) in enumerate(ranked[:8], start=1):
                    segment = {
                        "name": name,
                        "rank": idx if recent_value is not None else None,
                        "recent_share_pct": recent_share,
                        "series_pct": shares,
                        "value_series": values,
                    }
                    if recent_value is None:
                        segment["data_quality"] = {"available": False, "reason": "no_data"}
                    segments.append(segment)
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


def _ensure_split_class_alias(payload: dict[str, Any]) -> dict[str, Any]:
    """배포 포탈의 generic Class 계약을 split-class payload에도 보존한다.

    일부 시장(예: 악템라)은 MI Master 정의상 Class가 Class 1/Class 2로
    쪼개진다. 하지만 현재 배포된 포탈 번들은 방어 없이
    analysis_levels.data.Class를 읽는다. 프론트만 고치는 대안은 이미 배포된
    번들에는 효과가 없으므로, Class는 없고 split-class key가 있는 payload에
    상세 시각화 기준인 Class 2를 Class alias로 추가한다. Class 2가 없는
    payload만 Class 1로 대체한다. 원본 Class 1/Class 2는 데이터 구동
    셀렉터에 계속 필요하므로 삭제하지 않는다.
    """
    if not isinstance(payload, dict):
        return payload
    data = payload.get("data")
    if isinstance(data, dict) and "Class" not in data:
        if "Class 2" in data:
            data["Class"] = deepcopy(data["Class 2"])
        elif "Class 1" in data:
            data["Class"] = deepcopy(data["Class 1"])
    return payload


def _ensure_analysis_level_market_status_contract(payload: dict[str, Any]) -> dict[str, Any]:
    """배포 포탈이 기대하는 chart8형 analysis_level_market_status 계약을 보존한다.

    운영 포탈 번들은 이 카드도 chart8 렌더러로 그리기 때문에
    analysis_level_market_status.data[level].by_channel[channel]을 직접 읽는다.
    by_level/by_channel 래퍼를 별도로 내보내는 대안은 신규 구조처럼 보이지만,
    배포 번들에서는 data가 없어 흰 화면으로 죽으므로 기각한다. 이 함수는
    chart8형 data/levels/channels 구조를 유지하면서 split-class 시장에 필요한
    generic Class alias만 보강한다.
    """
    if not isinstance(payload, dict):
        return payload
    return _ensure_split_class_alias(payload)


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
    market_id = market.get("ml_id") or market.get("cd_id")
    if is_iron_iv_dimension_market(market_id) and FE_CONTENT_LEVEL not in levels:
        # Wave 3b: 철 시장의 Fe/ml은 MI Master의 IV pack overlay에서만
        # 만들어지는 시장 특수 dimension이다. 전역 level로 열면 타 시장 payload
        # 계약이 흔들리므로 strategy_012/cd_015에서만 노출한다.
        insert_at = levels.index("용량") + 1 if "용량" in levels else len(levels)
        levels.insert(insert_at, FE_CONTENT_LEVEL)
    return levels


def _class_level_axes(rows: list[dict[str, Any]] | None) -> list[str]:
    if not rows:
        return ["Class"]
    has_class_1 = False
    has_class_2 = False
    has_generic = False
    generic_equals_class_2 = True
    class_1_equals_class_2 = True
    for row in rows:
        generic = tuple(_dimension_values(row, "Class"))
        class_1 = tuple(_dimension_values(row, "Class 1"))
        class_2 = tuple(_dimension_values(row, "Class 2"))
        has_class_1 = has_class_1 or bool(class_1)
        has_class_2 = has_class_2 or bool(class_2)
        has_generic = has_generic or bool(generic)
        if generic or class_2:
            generic_equals_class_2 = generic_equals_class_2 and generic == class_2
        if class_1 or class_2:
            class_1_equals_class_2 = class_1_equals_class_2 and class_1 == class_2
    if not has_class_1 and not has_class_2:
        return ["Class"]
    if not has_generic:
        return [
            level
            for level, available in (("Class 1", has_class_1), ("Class 2", has_class_2))
            if available
        ]

    generic_equals_class_2 = has_class_2 and generic_equals_class_2
    class_1_equals_class_2 = has_class_1 and has_class_2 and class_1_equals_class_2
    if generic_equals_class_2:
        return [
            level
            for level, available in (("Class 1", has_class_1), ("Class 2", has_class_2))
            if available
        ]

    axes = ["Class"]
    if has_class_1:
        axes.append("Class 1")
    if has_class_2 and not class_1_equals_class_2:
        axes.append("Class 2")
    return axes


def _strategic_levels(
    market: dict[str, Any] | None,
    rows: list[dict[str, Any]] | None = None,
) -> list[str]:
    levels = _market_levels(market)
    if "Class" in levels:
        index = levels.index("Class")
        levels[index : index + 1] = _class_level_axes(rows)
    return levels


def _response_levels(
    market: dict[str, Any] | None,
    _view_source_id: str | None,
    rows: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Return only the analysis levels enabled by the market catalog."""
    enabled_levels = set(_strategic_levels(market, rows))
    return _ordered_response_levels(enabled_levels)


def _ordered_response_levels(enabled_levels: set[str]) -> list[str]:
    """Return response-order levels from an already-resolved enabled set."""
    enabled_levels.add("Brand")
    ordered_levels = ["Class", "Class 1", "Class 2", *CAUSE_LEVELS_V091[1:]]
    ordered_levels = [*ordered_levels, FISH_OIL_LEVEL]
    if FE_CONTENT_LEVEL in enabled_levels:
        insert_at = ordered_levels.index("용량") + 1 if "용량" in ordered_levels else len(ordered_levels)
        ordered_levels.insert(insert_at, FE_CONTENT_LEVEL)
    return [level for level in ordered_levels if level in enabled_levels]


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
        return [part.strip() for part in text.split("|") if part.strip() and not _is_excluded_dimension_label(part)]
    return [] if _is_excluded_dimension_label(text) else [text]


def _is_excluded_dimension_label(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    upper = text.upper()
    if upper in {"N/A", "NA", "#N/A", "NONE", "NULL", "NAN"}:
        return True
    return "제외" in text and not text.startswith("비제외")


def _is_class_level(level: str) -> bool:
    return str(level or "").strip().lower().replace("_", " ").startswith("class")


def _requires_unclassified_dimension_bucket(level: str) -> bool:
    return _is_class_level(level) or level == "Molecule"


def _row_is_class_excluded(row: dict[str, Any]) -> bool:
    if bool(row.get("is_class_excluded")):
        return True
    overlay = row.get("__overlay_data")
    if overlay is None:
        overlay = decode_json(row.get("overlay_data"))
        row["__overlay_data"] = overlay
    return isinstance(overlay, dict) and bool(overlay.get("is_class_excluded"))


def _dimension_value(row: dict[str, Any], level: str) -> str | None:
    values = _dimension_values(row, level)
    return values[0] if values else None


def _dimension_values(row: dict[str, Any], level: str) -> list[str]:
    dimension_values_cache = row.setdefault("__dimension_values_cache", {})
    if isinstance(dimension_values_cache, dict) and level in dimension_values_cache:
        return dimension_values_cache[level]
    if level == "Brand":
        value = row.get("brand_name") or row.get("brand_key")
        values = _split_atomic_dimension(level, value)
        dimension_values_cache[level] = values
        return values
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
            values = _split_atomic_dimension(level, value)
            dimension_values_cache[level] = values
            return values
    values = []
    dimension_values_cache[level] = values
    return values


def _dimension_series_map(row: dict[str, Any], field: str | None) -> dict[str, Any]:
    if not field:
        return {}
    series_map_cache = row.setdefault("__dimension_series_map_cache", {})
    if isinstance(series_map_cache, dict) and field in series_map_cache:
        return series_map_cache[field]
    dimension_data = row.get("__dimension_data")
    if dimension_data is None:
        dimension_data = decode_json(row.get("dimension_data"))
        row["__dimension_data"] = dimension_data
    if not isinstance(dimension_data, dict):
        series_map_cache[field] = {}
        return series_map_cache[field]
    series_map = dimension_data.get(field)
    if not isinstance(series_map, dict):
        series_map_cache[field] = {}
        return series_map_cache[field]
    values = {
        str(label): series
        for label, series in series_map.items()
        if not _is_excluded_dimension_label(label) and isinstance(series, dict)
    }
    series_map_cache[field] = values
    return values


def _has_dimension_field(row: dict[str, Any], field: str | None) -> bool:
    if not field:
        return False
    dimension_data = row.get("__dimension_data")
    if dimension_data is None:
        dimension_data = decode_json(row.get("dimension_data"))
        row["__dimension_data"] = dimension_data
    return isinstance(dimension_data, dict) and isinstance(dimension_data.get(field), dict)


def _dimension_channel_series_map(row: dict[str, Any], field: str | None, source: str, channel: str) -> dict[str, dict[str, Any]]:
    if not field or channel == "전체":
        return {}
    series_cache = row.setdefault("__dimension_channel_series_cache", {})
    cache_key = (field, source, channel)
    if isinstance(series_cache, dict) and cache_key in series_cache:
        return series_cache[cache_key]
    dimension_channel_data = row.get("__dimension_channel_data")
    if dimension_channel_data is None:
        dimension_channel_data = decode_json(row.get("dimension_channel_data"))
        row["__dimension_channel_data"] = dimension_channel_data
    if not isinstance(dimension_channel_data, dict):
        return {}
    field_data = dimension_channel_data.get(field)
    if not isinstance(field_data, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for label, channel_map in field_data.items():
        label_text = str(label).strip()
        if _is_excluded_dimension_label(label_text) or not isinstance(channel_map, dict):
            continue
        merged = {period: {"raw_value": 0.0} for period in _series_periods_from_channel_map(channel_map)}
        matched = False
        for raw_channel, series in channel_map.items():
            if not _channel_matches(raw_channel, source, channel) or not isinstance(series, dict):
                continue
            matched = True
            for period, item in series.items():
                merged.setdefault(period, {"raw_value": 0.0})
                merged[period]["raw_value"] += _value_from_period_item(item)
        if matched:
            result[label_text] = merged
    if isinstance(series_cache, dict):
        series_cache[cache_key] = result
    return result


def _has_dimension_channel_field(row: dict[str, Any], field: str | None) -> bool:
    if not field:
        return False
    dimension_channel_data = row.get("__dimension_channel_data")
    if dimension_channel_data is None:
        dimension_channel_data = decode_json(row.get("dimension_channel_data"))
        row["__dimension_channel_data"] = dimension_channel_data
    return isinstance(dimension_channel_data, dict) and isinstance(dimension_channel_data.get(field), dict)


def _dimension_specialty_series_map(row: dict[str, Any], field: str | None, channel: str) -> dict[str, dict[str, Any]]:
    if not field or channel == "전체":
        return {}
    specialty_cache = row.setdefault("__dimension_specialty_series_cache", {})
    cache_key = (field, channel)
    if isinstance(specialty_cache, dict) and cache_key in specialty_cache:
        return specialty_cache[cache_key]
    dimension_specialty_data = row.get("__dimension_specialty_data")
    if dimension_specialty_data is None:
        dimension_specialty_data = decode_json(row.get("dimension_specialty_data"))
        row["__dimension_specialty_data"] = dimension_specialty_data
    if not isinstance(dimension_specialty_data, dict):
        return {}
    field_data = dimension_specialty_data.get(field)
    if not isinstance(field_data, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for label, channel_map in field_data.items():
        label_text = str(label).strip()
        if _is_excluded_dimension_label(label_text) or not isinstance(channel_map, dict):
            continue
        series = channel_map.get(channel)
        if isinstance(series, dict):
            result[label_text] = series
    if isinstance(specialty_cache, dict):
        specialty_cache[cache_key] = result
    return result


def _has_dimension_specialty_field(row: dict[str, Any], field: str | None) -> bool:
    if not field or field not in SPECIALTY_DIMENSION_FIELDS:
        return False
    dimension_specialty_data = row.get("__dimension_specialty_data")
    if dimension_specialty_data is None:
        dimension_specialty_data = decode_json(row.get("dimension_specialty_data"))
        row["__dimension_specialty_data"] = dimension_specialty_data
    return isinstance(dimension_specialty_data, dict) and isinstance(dimension_specialty_data.get(field), dict)


def _coalesced_dimension_channel_series(
    row: dict[str, Any],
    field: str | None,
    source: str,
    channel: str,
) -> dict[str, dict[str, Any]]:
    specialty = (
        _dimension_specialty_series_map(row, field, channel)
        if source == "UBIST"
        else {}
    )
    channel_series = _dimension_channel_series_map(row, field, source, channel)
    if not specialty:
        return channel_series
    return {
        **channel_series,
        **specialty,
    }


def _series_periods_from_channel_map(channel_map: dict[str, Any]) -> list[str]:
    periods: set[str] = set()
    for series in channel_map.values():
        if isinstance(series, dict):
            periods.update(str(period) for period in series.keys())
    return sorted(periods, key=period_key)


def _channel_bucket(raw: Any, source: str) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if source == "UBIST":
        if "상급" in text:
            return "상급종병"
        if "종합" in text or text == "종병":
            return "종병"
        if text == "병원" or ("병원" in text and "치과" not in text):
            return "병원"
        if text == "의원":
            return "의원"
        if "보건소" in text or "보건" in text:
            return "보건소"
        if "기타" in text:
            return "기타"
        return None
    upper = text.upper()
    if upper in {"KHPA", "KCPA", "KPA"}:
        return upper
    return None


def _channel_matches(raw: Any, source: str, channel: str) -> bool:
    bucket = _channel_bucket(raw, source)
    if channel == UBIST_TGH_FACILITY_CHANNEL:
        return source == "UBIST" and bucket in UBIST_TGH_FACILITY_BUCKETS
    return bucket == channel


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
        value = _optional_row_value(item)
        return value if value is not None else 0.0
    return safe_float(item) or 0.0


def _optional_value_from_period_item(item: Any) -> float | None:
    if isinstance(item, dict):
        return _optional_row_value(item)
    return safe_float(item)


def _period_item_is_observed(item: Any) -> bool:
    if isinstance(item, dict):
        return any(
            key in item and safe_float(item.get(key)) is not None
            for key in ("raw_value", "value", "sales")
        )
    return safe_float(item) is not None


def _series_values(
    series: dict[str, Any],
    periods: list[str],
    series_value_cache: _SeriesValueCache | None,
) -> array:
    if series_value_cache is None:
        return array("d", (_value_from_period_item(series.get(period)) for period in periods))
    key = (id(series), tuple(periods))
    cached = series_value_cache.get(key)
    if cached is not None and cached[0] is series:
        return cached[1]
    values = array("d", (_value_from_period_item(series.get(period)) for period in periods))
    series_value_cache[key] = (series, values)
    return values


def _series_values_with_observed(
    series: dict[str, Any],
    periods: list[str],
    *,
    cache: _SeriesObservedCache | None = None,
) -> tuple[array, tuple[bool, ...]]:
    """Convert a period series once while preserving missing-vs-zero state."""
    key = (id(series), tuple(periods))
    if cache is not None:
        cached = cache.get(key)
        if cached is not None and cached[0] is series:
            return cached[1], cached[2]
    values = array("d")
    observed: list[bool] = []
    for period in periods:
        item = series.get(period)
        parsed = _optional_value_from_period_item(item)
        observed.append(parsed is not None)
        values.append(parsed if parsed is not None else 0.0)
    result = (values, tuple(observed))
    if cache is not None:
        cache[key] = (series, values, result[1])
    return result


def _add_series(
    target: dict[str, list[float]],
    series: dict[str, Any],
    periods: list[str],
    *,
    series_value_cache: _SeriesValueCache | None = None,
) -> None:
    for period, value in zip(periods, _series_values(series, periods, series_value_cache)):
        target[period][0] += value


def _latest_valid_share_pct(
    value_series: list[float | None],
    total_series: list[float | None],
) -> float:
    for value, total in reversed(list(zip(value_series, total_series))):
        if value and total:
            return round(value / total * 100, 4)
    return 0.0


def _segment_rows_for_level(
    *,
    rows: list[dict[str, Any]],
    level: str,
    periods: list[str],
    source: str,
    channel: str,
    target_name: str | None,
    top_n: int | None = 5,
    use_latest_valid_share: bool = False,
    series_value_cache: _SeriesValueCache | None = None,
    series_observed_cache: _SeriesObservedCache | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, list[float]]] = {}
    totals: dict[str, list[float]] = {period: [0.0] for period in periods}
    observed_periods = {period: False for period in periods}

    def add_observed_series(
        target: dict[str, list[float]],
        series: dict[str, Any],
        *,
        also_add_to: dict[str, list[float]] | None = None,
    ) -> None:
        values, observed = _series_values_with_observed(
            series,
            periods,
            cache=series_observed_cache,
        )
        for period, value, is_observed in zip(periods, values, observed):
            target[period][0] += value
            if also_add_to is not None:
                also_add_to[period][0] += value
            if is_observed:
                observed_periods[period] = True

    def add_observed_series_to_targets(
        targets: list[dict[str, list[float]]],
        series: dict[str, Any],
        *,
        also_add_to: dict[str, list[float]] | None = None,
    ) -> None:
        values, observed = _series_values_with_observed(
            series,
            periods,
            cache=series_observed_cache,
        )
        for period, value, is_observed in zip(periods, values, observed):
            if also_add_to is not None:
                also_add_to[period][0] += value
            for target in targets:
                target[period][0] += value
            if is_observed:
                observed_periods[period] = True

    for row in rows:
        if _is_class_level(level) and _row_is_class_excluded(row):
            continue
        field = LEVEL_FIELD_BY_LABEL.get(level)
        dimension_field_present = _has_dimension_field(row, field) if channel == "전체" else False
        dimension_series = _dimension_series_map(row, field) if channel == "전체" else {}
        dimension_specialty_present = (
            _has_dimension_specialty_field(row, field)
            if channel != "전체" and source == "UBIST"
            else False
        )
        dimension_specialty_series = (
            _coalesced_dimension_channel_series(row, field, source, channel)
            if dimension_specialty_present
            else {}
        )
        dual_channel_data = (
            _dual_channel_data(row, source, channel)
            if channel != "전체" and not dimension_specialty_present
            else None
        )
        use_dimension_channel = channel != "전체" and not isinstance(dual_channel_data, dict)
        dimension_channel_present = _has_dimension_channel_field(row, field) if use_dimension_channel else False
        dimension_channel_series = _dimension_channel_series_map(row, field, source, channel) if use_dimension_channel else {}
        active_dimension_series = dimension_series if channel == "전체" else (dimension_specialty_series or dimension_channel_series)
        if active_dimension_series:
            for name, series in active_dimension_series.items():
                grouped.setdefault(name, {period: [0.0] for period in periods})
                add_observed_series(grouped[name], series, also_add_to=totals)
            continue
        if dimension_field_present and dimension_series:
            continue
        if dimension_specialty_present and dimension_specialty_series:
            continue
        if dimension_channel_present and dimension_channel_series:
            continue

        names = _dimension_values(row, level)
        if not names:
            if not _requires_unclassified_dimension_bucket(level):
                continue
            names = [UNCLASSIFIED_DIMENSION_NAME]
        if isinstance(dual_channel_data, dict) and len(names) != 1:
            continue
        targets = []
        for name in names:
            targets.append(grouped.setdefault(name, {period: [0.0] for period in periods}))
        if channel == "전체":
            history = _metric_history(row)
            if history:
                add_observed_series_to_targets(targets, history, also_add_to=totals)
            continue

        if isinstance(dual_channel_data, dict):
            add_observed_series_to_targets(targets, dual_channel_data, also_add_to=totals)
            continue

        channel_data = row.get("__channel_data")
        if channel_data is None:
            channel_data = decode_json(row.get("channel_data"))
            row["__channel_data"] = channel_data
        if not isinstance(channel_data, dict):
            continue
        for raw_channel, series in channel_data.items():
            if not _channel_matches(raw_channel, source, channel):
                continue
            if isinstance(series, dict):
                add_observed_series_to_targets(targets, series, also_add_to=totals)

    latest_observed_period = next(
        (period for period in reversed(periods) if observed_periods[period]),
        None,
    )

    ranked = sorted(
        grouped.items(),
        key=lambda item: item[1][latest_observed_period][0] if latest_observed_period else 0.0,
        reverse=True,
    )
    if target_name:
        ranked = sorted(
            ranked,
            key=lambda item: (
                item[0] != target_name,
                -(item[1][latest_observed_period][0] if latest_observed_period else 0.0),
            ),
        )
    selected = ranked if top_n is None else ranked[:top_n]

    segments: list[dict[str, Any]] = []
    missing_periods = [period for period in periods if not observed_periods[period]]
    for rank, (name, series_map) in enumerate(selected, start=1):
        value_series = [
            round(series_map[period][0], 4) if observed_periods[period] else None
            for period in periods
        ]
        series_pct = []
        for period, value in zip(periods, value_series):
            if value is None:
                series_pct.append(None)
                continue
            total = totals[period][0]
            series_pct.append(round(value / total * 100, 4) if total else 0.0)
        segment = {
                "name": name,
                "rank": rank,
                "recent_share_pct": (
                    None
                    if latest_observed_period is None
                    else _latest_valid_share_pct(
                        value_series,
                        [totals[period][0] if observed_periods[period] else None for period in periods],
                    )
                    if use_latest_valid_share
                    else series_pct[-1] if series_pct else None
                ),
                "series_pct": series_pct,
                "value_series": value_series,
            }
        if name == UNCLASSIFIED_DIMENSION_NAME:
            segment["data_quality"] = {
                "available": False,
                "reason": "dimension_value_missing",
                "dimension": level,
            }
            if missing_periods:
                segment["data_quality"]["missing_periods"] = missing_periods
        elif missing_periods:
            segment["data_quality"] = {
                "available": False,
                "reason": "dimension_period_missing",
                "missing_periods": missing_periods,
            }
        segments.append(segment)
    return segments


def _rows_for_channel(
    rows: list[dict[str, Any]],
    source: str,
    channel: str,
    periods: list[str],
    *,
    series_value_cache: _SeriesValueCache | None = None,
) -> list[dict[str, Any]]:
    if channel == "전체":
        return rows

    filtered: list[dict[str, Any]] = []
    for row in rows:
        history = {period: 0.0 for period in periods}
        dual_channel_data = _dual_channel_data(row, source, channel)
        if isinstance(dual_channel_data, dict):
            for period, value in zip(periods, _series_values(dual_channel_data, periods, series_value_cache)):
                history[period] += value
        else:
            channel_data = row.get("__channel_data")
            if channel_data is None:
                channel_data = decode_json(row.get("channel_data"))
                row["__channel_data"] = channel_data
            if isinstance(channel_data, dict):
                for raw_channel, series in channel_data.items():
                    if not _channel_matches(raw_channel, source, channel) or not isinstance(series, dict):
                        continue
                    for period, value in zip(periods, _series_values(series, periods, series_value_cache)):
                        history[period] += value

        clone = dict(row)
        clone["metric_history"] = history
        clone["__metric_history"] = history
        clone.pop("__latest_history_item", None)
        clone["__series_cache"] = {
            (tuple(periods), True): [history[period] for period in periods],
        }
        filtered.append(clone)
    return filtered


def _history_from_period_series(
    series: dict[str, Any],
    periods: list[str],
    *,
    series_value_cache: _SeriesValueCache | None = None,
) -> dict[str, dict[str, float]]:
    return {
        period: {"raw_value": value}
        for period, value in zip(periods, _series_values(series, periods, series_value_cache))
    }


def _clone_with_metric_history(row: dict[str, Any], history: dict[str, Any]) -> dict[str, Any]:
    clone = dict(row)
    clone["metric_history"] = history
    clone["__metric_history"] = history
    clone.pop("__latest_history_item", None)
    clone.pop("__series_cache", None)
    return clone


def _rows_for_dimension(
    rows: list[dict[str, Any]],
    level: str,
    segment_name: str | None,
    periods: list[str],
    *,
    source: str | None = None,
    channel: str = "전체",
    series_value_cache: _SeriesValueCache | None = None,
) -> list[dict[str, Any]]:
    if segment_name in (None, "", "전체"):
        return rows
    if _is_excluded_dimension_label(segment_name):
        return []

    field = LEVEL_FIELD_BY_LABEL.get(level)
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if _is_class_level(level) and _row_is_class_excluded(row):
            continue

        dimension_present = _has_dimension_field(row, field) if channel == "전체" else False
        dimension_series = _dimension_series_map(row, field) if channel == "전체" else {}
        source_text = source or str(row.get("source") or "")
        dimension_specialty_present = (
            _has_dimension_specialty_field(row, field)
            if channel != "전체" and source_text == "UBIST"
            else False
        )
        dimension_specialty_series = (
            _coalesced_dimension_channel_series(row, field, source_text, channel)
            if dimension_specialty_present
            else {}
        )
        dual_channel_data = (
            _dual_channel_data(row, source_text, channel)
            if channel != "전체" and not dimension_specialty_present
            else None
        )
        use_dimension_channel = channel != "전체" and not isinstance(dual_channel_data, dict)
        dimension_channel_present = _has_dimension_channel_field(row, field) if use_dimension_channel else False
        dimension_channel_series = (
            _dimension_channel_series_map(row, field, source_text, channel)
            if use_dimension_channel
            else {}
        )
        active_dimension_series = dimension_series if channel == "전체" else (dimension_specialty_series or dimension_channel_series)
        if active_dimension_series:
            series = active_dimension_series.get(str(segment_name))
            if isinstance(series, dict):
                filtered.append(
                    _clone_with_metric_history(
                        row,
                        _history_from_period_series(
                            series,
                            periods,
                            series_value_cache=series_value_cache,
                        ),
                    )
                )
            continue
        if (
            (dimension_present and dimension_series)
            or (dimension_specialty_present and dimension_specialty_series)
            or (dimension_channel_present and dimension_channel_series)
        ):
            continue

        if str(segment_name) not in _dimension_values(row, level):
            continue
        if isinstance(dual_channel_data, dict) and len(_dimension_values(row, level)) != 1:
            continue
        if channel == "전체":
            filtered.append(row)
            continue
        if not source:
            continue
        if isinstance(dual_channel_data, dict):
            filtered.append(
                _clone_with_metric_history(
                    row,
                    _history_from_period_series(
                        dual_channel_data,
                        periods,
                        series_value_cache=series_value_cache,
                    ),
                )
            )
            continue
        for channel_row in _rows_for_channel(
            [row],
            source,
            channel,
            periods,
            series_value_cache=series_value_cache,
        ):
            history = _metric_history(channel_row)
            if any(_value_from_period_item(history.get(period)) for period in periods):
                filtered.append(channel_row)
    return filtered


def _rows_for_dimension_segments(
    rows: list[dict[str, Any]],
    level: str,
    periods: list[str],
    *,
    series_value_cache: _SeriesValueCache | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Index rows for every dimension segment in one pass.

    The analysis-level trend builder asks for several segments from the same
    row set. For the overall channel, the segment membership and optional
    dimension-sidecar history can be materialized once without changing the
    row-selection contract of ``_rows_for_dimension``.
    """

    field = LEVEL_FIELD_BY_LABEL.get(level)
    indexed: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if _is_class_level(level) and _row_is_class_excluded(row):
            continue

        dimension_series = _dimension_series_map(row, field)
        if dimension_series:
            for segment_name, series in dimension_series.items():
                indexed.setdefault(segment_name, []).append(
                    _clone_with_metric_history(
                        row,
                        _history_from_period_series(
                            series,
                            periods,
                            series_value_cache=series_value_cache,
                        ),
                    )
                )
            continue
        for segment_name in _dimension_values(row, level):
            indexed.setdefault(segment_name, []).append(row)
    return indexed


def _total_series_for_rows(rows: list[dict[str, Any]], periods: list[str]) -> list[float]:
    totals = [0.0 for _ in periods]
    for row in rows:
        series = _series_for_row(row, periods, scaled_sales=True)
        for idx, value in enumerate(series):
            totals[idx] += value
    return [round(value, 4) for value in totals]


def _dimension_specialty_total_series(
    *,
    rows: list[dict[str, Any]],
    level: str,
    source: str,
    channel: str,
    periods: list[str],
    series_value_cache: _SeriesValueCache | None = None,
) -> list[float] | None:
    if source != "UBIST" or channel == "전체":
        return None
    field = LEVEL_FIELD_BY_LABEL.get(level)
    if not field or field not in SPECIALTY_DIMENSION_FIELDS:
        return None

    totals = [0.0 for _ in periods]
    found = False
    for row in rows:
        if _is_class_level(level) and _row_is_class_excluded(row):
            continue
        if _has_dimension_specialty_field(row, field):
            series_by_label = _dimension_specialty_series_map(row, field, channel)
            if not series_by_label:
                continue
            found = True
            for series in series_by_label.values():
                for idx, value in enumerate(_series_values(series, periods, series_value_cache)):
                    totals[idx] += value
            continue

        dual_channel_data = _dual_channel_data(row, source, channel)
        dimension_values = _dimension_values(row, level)
        if not isinstance(dual_channel_data, dict) or len(dimension_values) != 1:
            continue
        if _is_excluded_dimension_label(dimension_values[0]):
            continue
        found = True
        for idx, value in enumerate(_series_values(dual_channel_data, periods, series_value_cache)):
            totals[idx] += value
    if not found:
        return None
    return [round(value, 4) for value in totals]


def _sum_segment_value_series(
    segments: list[dict[str, Any]],
    periods: list[str],
) -> list[float | None]:
    totals: list[float | None] = [0.0 for _ in periods]
    for segment in segments:
        if not isinstance(segment, dict) or segment.get("is_overall"):
            continue
        series = list(segment.get("value_series") or [])
        if len(series) != len(periods):
            series = series[-len(periods):] if periods else []
        for idx, value in enumerate(series):
            if idx < len(totals):
                parsed = safe_float(value)
                if parsed is None:
                    totals[idx] = None
                elif totals[idx] is not None:
                    totals[idx] += parsed
    return [round(value, 4) if value is not None else None for value in totals]


def _series_covers_options(
    total_series: list[float],
    option_series: list[float | None],
) -> bool:
    if not total_series or len(total_series) != len(option_series):
        return False
    return all(
        option is not None and (total + 1.0) >= option
        for total, option in zip(total_series, option_series)
    )


def _with_overall_level_options(
    *,
    data: dict[str, Any],
    rows: list[dict[str, Any]],
    source: str,
    channels: list[str],
    periods: list[str],
    series_value_cache: _SeriesValueCache | None = None,
) -> dict[str, Any]:
    channel_rows_cache: dict[str, list[dict[str, Any]]] = {}
    channel_total_series_cache: dict[str, list[float]] = {}
    for level, level_data in data.items():
        if not isinstance(level_data, dict):
            continue
        by_channel = level_data.get("by_channel")
        if not isinstance(by_channel, dict):
            continue
        for channel in channels:
            segments = by_channel.get(channel)
            if not isinstance(segments, list) or not segments:
                continue
            if any(isinstance(segment, dict) and segment.get("name") == "전체" for segment in segments):
                continue
            if channel not in channel_rows_cache:
                channel_rows_cache[channel] = _rows_for_channel(
                    rows,
                    source,
                    channel,
                    periods,
                    series_value_cache=series_value_cache,
                )
                channel_total_series_cache[channel] = _total_series_for_rows(
                    channel_rows_cache[channel],
                    periods,
                )
            channel_total_series = channel_total_series_cache[channel]
            option_sum_series = _sum_segment_value_series(segments, periods)
            exact_dimension_total_series = _dimension_specialty_total_series(
                rows=rows,
                level=level,
                source=source,
                channel=channel,
                periods=periods,
                series_value_cache=series_value_cache,
            )
            value_series = (
                exact_dimension_total_series
                if exact_dimension_total_series
                and _series_covers_options(exact_dimension_total_series, option_sum_series)
                else channel_total_series
            )
            by_channel[channel] = [
                {
                    "name": "전체",
                    "rank": 0,
                    "value_series": value_series,
                    "is_overall": True,
                },
                *segments,
            ]
        if isinstance(by_channel.get("전체"), list):
            level_data["segments"] = by_channel["전체"]
    return data


def _with_ms_level_options(data: dict[str, Any]) -> dict[str, Any]:
    for level_data in data.values():
        if not isinstance(level_data, dict):
            continue
        by_channel = level_data.get("by_channel")
        if not isinstance(by_channel, dict):
            continue
        ms_by_channel: dict[str, list[dict[str, Any]]] = {}
        for channel, segments in by_channel.items():
            if not isinstance(segments, list):
                continue
            ms_by_channel[channel] = [
                deepcopy(segment)
                for segment in segments
                if isinstance(segment, dict) and not segment.get("is_overall")
            ]
        level_data["ms_by_channel"] = ms_by_channel
        if isinstance(ms_by_channel.get("전체"), list):
            level_data["ms_segments"] = ms_by_channel["전체"]
    return data


def _build_analysis_levels_from_mart(
    *,
    rows: list[dict[str, Any]],
    source: str,
    market: dict[str, Any] | None,
    view_source_id: str | None,
    target_name: str | None,
    fallback_level_top5: dict[str, Any],
    channels_override: list[str] | None = None,
    use_latest_valid_share: bool = False,
    series_value_cache: _SeriesValueCache | None = None,
    series_observed_cache: _SeriesObservedCache | None = None,
    resolved_levels: set[str] | None = None,
    resolved_periods: list[str] | None = None,
) -> dict[str, Any]:
    if series_value_cache is None:
        series_value_cache = {}
    if series_observed_cache is None:
        series_observed_cache = {}
    if resolved_levels is None:
        level_resolution_started = perf_counter() if _latency_stage_timing_enabled() else 0.0
        enabled_levels = set(_strategic_levels(market, rows))
        if level_resolution_started:
            logger.info(
                "market_latency_analysis_resolve_levels levels=%s rows=%s ms=%.3f",
                len(enabled_levels),
                len(rows),
                (perf_counter() - level_resolution_started) * 1000,
            )
    else:
        enabled_levels = set(resolved_levels)
    levels = _ordered_response_levels(enabled_levels)
    if resolved_periods is None:
        periods_started = perf_counter() if _latency_stage_timing_enabled() else 0.0
        periods = _history_periods(rows, source)
        if periods_started:
            logger.info(
                "market_latency_analysis_history_periods periods=%s rows=%s ms=%.3f",
                len(periods),
                len(rows),
                (perf_counter() - periods_started) * 1000,
            )
    else:
        periods = list(resolved_periods)
    data: dict[str, Any] = {}
    channels = channels_override or _channels_for_source(source)
    for level in levels:
        level_started = perf_counter() if _latency_stage_timing_enabled() else 0.0
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
                    use_latest_valid_share=use_latest_valid_share,
                    series_value_cache=series_value_cache,
                    series_observed_cache=series_observed_cache,
                )
                for channel in channels
            }
        else:
            by_channel = {channel: [] for channel in channels}
        data[level] = {"segments": by_channel["전체"], "by_channel": by_channel}
        if level_started:
            logger.info(
                "market_latency_analysis_level level=%s channels=%s ms=%.3f",
                level,
                len(channels),
                (perf_counter() - level_started) * 1000,
            )
    overall_started = perf_counter() if _latency_stage_timing_enabled() else 0.0
    data = _with_overall_level_options(
        data=data,
        rows=rows,
        source=source,
        channels=channels,
        periods=periods,
        series_value_cache=series_value_cache,
    )
    if overall_started:
        logger.info(
            "market_latency_analysis_overall_options levels=%s channels=%s ms=%.3f",
            len(levels),
            len(channels),
            (perf_counter() - overall_started) * 1000,
        )
    data = _with_ms_level_options(data)
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
        ms_by_channel = level_data.get("ms_by_channel")
        if isinstance(ms_by_channel, dict):
            for channel, segments in list(ms_by_channel.items()):
                if isinstance(segments, list):
                    ms_by_channel[channel] = segments[:limit]
            if isinstance(ms_by_channel.get("전체"), list):
                level_data["ms_segments"] = ms_by_channel["전체"]
    return trimmed


def _growth_ms_matrix(ei_rows: Any) -> dict[str, Any]:
    rows = ei_rows if isinstance(ei_rows, list) else []
    output = []
    for row in rows:
        share = safe_float(row.get("ms") or row.get("share_pct"))
        contribution = safe_float(
            matrix_growth_value(
                optional_float(row.get("growth_contribution")),
                optional_float(row.get("contribution_pct")),
                optional_float(row.get("momentum_score")),
            )
        )
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


def _optional_series_for_row(
    row: dict[str, Any],
    periods: list[str],
    *,
    scaled_sales: bool,
) -> list[float | None]:
    cache_key = ("optional", tuple(periods), scaled_sales)
    series_cache = row.setdefault("__series_cache", {})
    if cache_key in series_cache:
        return series_cache[cache_key]
    history = _metric_history(row)
    values = [
        round(value, 4) if (value := _optional_value_from_period_item(history.get(period))) is not None else None
        for period in periods
    ]
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
        value_recent = _first_optional_float(recent.get("raw_value"), recent.get("value"))
        share = safe_float(recent.get("ms"))
        cache_key = (ei_market_key if ei_market_key is not None else id(market_series), row.get("id") or row.get("brand_key") or brand)
        if cache_key not in EI_META_CACHE:
            EI_META_CACHE[cache_key] = calculate_ei_with_fallback(_metric_history(row), market_series)
        ei_meta = EI_META_CACHE[cache_key]
        cagr_5y = first_float(extended.get("cagr_5y"))
        cagr_5y_pct = round(cagr_5y * 100, 4) if cagr_5y is not None else None
        ei_5y = optional_float(ei_meta.get("ei"))
        momentum_score = first_float(extended.get("momentum_score"))
        parsed_growth_contribution = safe_float(
            matrix_growth_value(
                optional_float(extended.get("growth_contribution")),
                optional_float(extended.get("growth_contribution_pct")),
                optional_float(extended.get("momentum_score")),
            )
        )
        growth_contribution = parsed_growth_contribution
        item = {
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
        if value_recent is None:
            item["data_quality"] = {"available": False, "reason": "no_data"}
        normalized.append(item)

    market_total = optional_float(series_latest_number(market_series)) if market_series else None
    if market_total is None or market_total <= 0:
        market_total = _sum_optional_complete(row["value_recent"] for row in normalized)
    if market_total and market_total > 0:
        for row in normalized:
            value_recent = safe_float(row.get("value_recent"))
            share = round(value_recent / market_total * 100, 4) if value_recent is not None else None
            row["share_pct"] = share
            row["ms_pct"] = share
            row["ms_recent_pct"] = share

    ranked = [
        row
        for row in sorted(
            normalized,
            key=lambda item: (
                safe_float(item.get("value_recent")) is not None,
                safe_float(item.get("value_recent")) or 0.0,
            ),
            reverse=True,
        )
        if safe_float(row.get("value_recent")) is not None and safe_float(row.get("value_recent")) > 0
    ]
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index

    target = next((row for row in normalized if row["is_target"]), None)
    target_id = row_identity(target, "brand")
    competitors = [
        row
        for row in sorted(
            normalized,
            key=lambda item: (
                safe_float(item.get("value_recent")) is not None,
                safe_float(item.get("value_recent")) or 0.0,
            ),
            reverse=True,
        )
        if row_identity(row, "brand") != target_id
    ]
    # B1: 채널/세그먼트 내 표시 브랜드도 선택 브랜드를 선두에 고정하고,
    # 경쟁 top5와 기타를 뒤에 붙인다. 선택 브랜드가 competitors에 이미 있으면
    # target_id로 제거해 중복을 막는다. 기타에 선택 브랜드를 남기는 대안은
    # double counting을 만들었기 때문에 기각했다.
    selected = ([target] if target else []) + competitors[:top_n]
    selected_ids = {row_identity(row, "brand") for row in selected}
    others = [row for row in normalized if row_identity(row, "brand") not in selected_ids]
    if include_others and others:
        selected_ms = _sum_optional_complete(row["ms_pct"] for row in selected)
        selected_contribution = _sum_optional_complete(row["contribution_pct"] for row in selected)
        others_value = _sum_optional_complete(row["value_recent"] for row in others)
        others_contribution = _sum_optional_complete(row["growth_contribution"] for row in others)
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
                "value_recent": others_value,
                "raw_value": others_value,
                "share_pct": round(max(0.0, 100.0 - selected_ms), 4) if selected_ms is not None else None,
                "ms_pct": round(max(0.0, 100.0 - selected_ms), 4) if selected_ms is not None else None,
                "ms_recent_pct": round(max(0.0, 100.0 - selected_ms), 4) if selected_ms is not None else None,
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
                "growth_contribution": others_contribution,
                "growth_contribution_pct": round(100.0 - selected_contribution, 4) if selected_contribution is not None else None,
                "contribution": others_contribution,
                "contribution_pct": round(100.0 - selected_contribution, 4) if selected_contribution is not None else None,
            }
        )
        if others_value is None or others_contribution is None:
            selected[-1]["data_quality"] = {"available": False, "reason": "no_data"}
    return [{key: value for key, value in row.items() if key != "_source_row"} for row in selected]


def _matrix_payload(entries: list[dict[str, Any]]) -> dict[str, Any]:
    visible = [row for row in entries if not row.get("is_others")]
    shares = [share for row in visible if (share := safe_float(row.get("share_pct"))) is not None]
    avg = round(sum(shares) / len(shares), 4) if shares else None
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
            value = _first_optional_float(
                item.get(value_key),
                item.get("hhi"),
                item.get("company_hhi"),
                item.get("cr4"),
            )
        else:
            value = safe_float(item)
        point = {"period": period, "period_full": period, "year": year, value_key: value}
        if value is None:
            point["data_quality"] = {"available": False, "reason": "no_data"}
        points.append(point)
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
    incomplete_years: set[int] = set()
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
            value = _optional_row_value(row)
            if value is None:
                incomplete_years.add(year)
                continue
            by_year[year][str(name)] += value
    points = []
    for year in sorted(by_year.keys())[-5:]:
        values = by_year[year]
        total = sum(values.values())
        hhi = (
            sum(((value / total) * 100.0) ** 2 for value in values.values())
            if total > 0 and year not in incomplete_years
            else None
        )
        point = {"period": str(year), "period_full": period_by_year.get(year, str(year)), "year": year, "hhi": round(hhi, 4) if hhi is not None else None}
        if hhi is None:
            point["data_quality"] = {"available": False, "reason": "no_data"}
        points.append(point)
    return points


def _complete_calendar_years(period_count_by_year: dict[int, int], *, source: str | None) -> set[int]:
    """Return years that have the full source-specific calendar coverage.

    무엇/왜: HHI는 연간 시장 집중도를 비교하는 지표라 12개월(UBIST) 또는
    4분기(IQVIA)를 모두 채운 달력연만 서로 비교 가능하다. 2026년처럼
    4개월 partial-year를 넣으면 몇 개 브랜드의 초기기간 집중도가 과장돼
    HHI가 302처럼 튀는 왜곡이 생겼다.
    도메인 근거: UBIST는 월 단위, IQVIA는 분기 단위로 적재된다. 연도
    하드코딩 대신 실제 period count로 판정해야 다음 사이클에서도 자동으로
    2022~2026 등 최근 5개 완전연으로 이동한다.
    기각한 대안: partial year를 연율화하거나 최신 5개 연도를 무조건 쓰는
    방식은 동일 길이 관측치 비교가 아니어서 HHI 추세 해석을 깨뜨린다.
    """
    source_key = str(source or "").upper()
    expected = 12 if source_key == "UBIST" else 4 if source_key == "IQVIA" else None
    if expected is None:
        return set(period_count_by_year)
    return {year for year, count in period_count_by_year.items() if count >= expected}


def _annual_share_hhi_from_rows(
    rows: list[dict[str, Any]],
    *,
    label_key: str,
    source: str | None = None,
    annual_rank_cache: _AnnualRankRowsCache | None = None,
) -> list[dict[str, Any]]:
    by_year, period_count_by_year = _annual_rank_rows(
        {},
        label_key=label_key,
        target_name=None,
        full_rows=rows,
        annual_rank_cache=annual_rank_cache,
    )
    complete_years = _complete_calendar_years(period_count_by_year, source=source)
    points = []
    for year in sorted(year for year in by_year.keys() if year in complete_years)[-5:]:
        year_rows = by_year[year]
        shares = [safe_float(row.get("ms_pct")) for row in year_rows]
        hhi = sum(share**2 for share in shares if share is not None) if all(share is not None for share in shares) else None
        point = {"period": str(year), "period_full": str(year), "year": year, "hhi": round(hhi, 4) if hhi is not None else None}
        if hhi is None:
            point["data_quality"] = {"available": False, "reason": "no_data"}
        points.append(point)
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
    values: list[float | None] = []
    data_quality: list[dict[str, Any] | None] = []
    for year in sorted(by_year.keys())[-5:]:
        _, rows = by_year[year]
        shares = [_optional_row_share(row) for row in rows]
        hhi = sum(share**2 for share in shares if share is not None) if all(share is not None for share in shares) else None
        periods.append(str(year))
        values.append(round(hhi, 4) if hhi is not None else None)
        data_quality.append(None if hhi is not None else {"available": False, "reason": "no_data"})
    result = {"periods": periods, "hhi_values": values}
    if any(item is not None for item in data_quality):
        result["data_quality"] = data_quality
    return result


def _company_hhi_from_rows(
    rows: list[dict[str, Any]],
    *,
    source: str | None = None,
    annual_rank_cache: _AnnualRankRowsCache | None = None,
) -> dict[str, Any]:
    points = _annual_share_hhi_from_rows(
        rows,
        label_key="company",
        source=source,
        annual_rank_cache=annual_rank_cache,
    )
    return {
        "periods": [str(point["year"]) for point in points],
        "hhi_values": [
            round(value, 4) if (value := safe_float(point.get("hhi"))) is not None else None
            for point in points
        ],
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
                "complete": True,
            },
        )
        bucket["brands"].append(row.get("brand"))
        bucket["is_target"] = bucket["is_target"] or bool(target_company and company == target_company)
        bucket["is_jw"] = bucket["is_jw"] or bool(row.get("is_jw"))
        contribution = safe_float(row.get("growth_contribution"))
        contribution_pct = safe_float(row.get("growth_contribution_pct"))
        value_recent = safe_float(row.get("value_recent"))
        if contribution is None or contribution_pct is None or value_recent is None:
            bucket["complete"] = False
            bucket["contribution"] = None
            bucket["contribution_pct"] = None
            bucket["value_recent"] = None
        elif bucket["complete"]:
            bucket["contribution"] += contribution
            bucket["contribution_pct"] += contribution_pct
            bucket["value_recent"] += value_recent
    rows = list(grouped.values())
    for row in rows:
        row.pop("complete", None)
        if row["value_recent"] is None:
            row["data_quality"] = {"available": False, "reason": "no_data"}
    target = next((row for row in rows if row["is_target"]), None)
    competitors = [row for row in sorted(rows, key=lambda item: (safe_float(item.get("value_recent")) is not None, safe_float(item.get("value_recent")) or 0.0), reverse=True) if row is not target]
    selected = ([target] if target else []) + competitors[:5]
    rest = [row for row in rows if row not in selected]
    if rest:
        rest_contribution = _sum_optional_complete(row["contribution"] for row in rest)
        rest_pct = _sum_optional_complete(row["contribution_pct"] for row in rest)
        rest_value = _sum_optional_complete(row["value_recent"] for row in rest)
        selected.append(
            {
                "company": "기타",
                "brands": [brand for row in rest for brand in row.get("brands", [])],
                "is_target": False,
                "is_jw": False,
                "is_others": True,
                "contribution": rest_contribution,
                "contribution_pct": rest_pct,
                "value_recent": rest_value,
            }
        )
        if rest_value is None or rest_contribution is None or rest_pct is None:
            selected[-1]["data_quality"] = {"available": False, "reason": "no_data"}
    return {"top_contributors": selected, "others_total": 0.0}


def _history_value_at(row: dict[str, Any], period: str | None) -> float | None:
    if not period:
        return None
    history = _metric_history(row)
    return _optional_value_from_period_item(history.get(period)) if isinstance(history, dict) else None


def _top_contribution_rows(
    rows: list[dict[str, Any]],
    target_name: str | None,
    periods: list[str],
    top_n: int = 5,
) -> tuple[list[dict[str, Any]], float | None, float | None, float | None]:
    period_start = periods[0] if periods else None
    period_end = periods[-1] if periods else None
    period_values = [
        (_history_value_at(row, period_start), _history_value_at(row, period_end))
        for row in rows
    ]
    complete_market = all(start is not None and end is not None for start, end in period_values)
    market_start = sum(start for start, _ in period_values if start is not None) if complete_market else None
    market_end = sum(end for _, end in period_values if end is not None) if complete_market else None
    market_growth = market_end - market_start if market_start is not None and market_end is not None else None
    contribution_rows: list[dict[str, Any]] = []
    for row in rows:
        brand = _row_brand(row)
        if not brand:
            continue
        start_value = _history_value_at(row, period_start)
        end_value = _history_value_at(row, period_end)
        value = end_value - start_value if start_value is not None and end_value is not None else None
        pct = round(value / market_growth * 100, 4) if value is not None and market_growth else None
        item = {
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
        if value is None:
            item["data_quality"] = {"available": False, "reason": "no_data"}
        contribution_rows.append(item)

    target = next((row for row in contribution_rows if row["is_target"]), None)
    competitors = [
        row
        for row in sorted(
            contribution_rows,
            key=lambda item: (
                safe_float(item.get("contribution_value")) is not None,
                abs(safe_float(item.get("contribution_value")) or 0.0),
            ),
            reverse=True,
        )
        if row is not target
    ]
    selected = ([target] if target else []) + competitors[:top_n]
    rest = [row for row in contribution_rows if row not in selected]
    if rest:
        displayed_values = [safe_float(row.get("contribution_pct")) for row in selected]
        rest_values = [safe_float(row.get("contribution_value")) for row in rest]
        rest_starts = [safe_float(row.get("value_start")) for row in rest]
        rest_ends = [safe_float(row.get("value_end")) for row in rest]
        displayed_complete = all(value is not None for value in displayed_values)
        rest_complete = all(value is not None for value in (*rest_values, *rest_starts, *rest_ends))
        selected.append(
            {
                "brand": "기타",
                "company": f"{len(rest)}개 brand",
                "is_target": False,
                "is_jw": False,
                "is_others": True,
                "contribution": sum(value for value in rest_values if value is not None) if rest_complete else None,
                "contribution_value": sum(value for value in rest_values if value is not None) if rest_complete else None,
                "contribution_pct": (
                    round(100.0 - sum(value for value in displayed_values if value is not None), 4)
                    if market_growth and displayed_complete and rest_complete
                    else None
                ),
                "value_start": sum(value for value in rest_starts if value is not None) if rest_complete else None,
                "value_end": sum(value for value in rest_ends if value is not None) if rest_complete else None,
                "value_recent": sum(value for value in rest_ends if value is not None) if rest_complete else None,
            }
        )
        if not rest_complete:
            selected[-1]["data_quality"] = {"available": False, "reason": "no_data"}
    return selected, market_start, market_end, market_growth


def _company_contribution_payload(rows: list[dict[str, Any]], target_company: str | None, periods: list[str], market_growth: float | None, top_n: int = 5) -> dict[str, Any]:
    period_start = periods[0] if periods else None
    period_end = periods[-1] if periods else None
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        company = _company_name(row)
        bucket = grouped.setdefault(company, {"company": company, "brands": [], "value_start": 0.0, "value_end": 0.0, "complete": True, "is_target": bool(target_company and company == target_company), "is_jw": False})
        bucket["brands"].append(_row_brand(row))
        start_value = _history_value_at(row, period_start)
        end_value = _history_value_at(row, period_end)
        if start_value is None or end_value is None:
            bucket["complete"] = False
        else:
            bucket["value_start"] += start_value
            bucket["value_end"] += end_value
        bucket["is_jw"] = bucket["is_jw"] or bool(row.get("is_jw"))
    company_rows = []
    for bucket in grouped.values():
        value = bucket["value_end"] - bucket["value_start"] if bucket["complete"] else None
        item = {
                "company": bucket["company"],
                "brands": bucket["brands"],
                "is_target": bucket["is_target"],
                "is_jw": bucket["is_jw"],
                "is_others": False,
                "contribution": value,
                "contribution_value": value,
                "contribution_pct": round(value / market_growth * 100, 4) if value is not None and market_growth else None,
                "value_recent": bucket["value_end"] if bucket["complete"] else None,
            }
        if value is None:
            item["data_quality"] = {"available": False, "reason": "no_data"}
        company_rows.append(item)
    target = next((row for row in company_rows if row["is_target"]), None)
    competitors = [row for row in sorted(company_rows, key=lambda item: (safe_float(item.get("contribution_value")) is not None, abs(safe_float(item.get("contribution_value")) or 0.0)), reverse=True) if row is not target]
    selected = ([target] if target else []) + competitors[:top_n]
    rest = [row for row in company_rows if row not in selected]
    if rest:
        displayed_pct = _sum_optional_complete(row.get("contribution_pct") for row in selected)
        rest_contribution = _sum_optional_complete(row.get("contribution_value") for row in rest)
        rest_value = _sum_optional_complete(row.get("value_recent") for row in rest)
        selected.append(
            {
                "company": "기타",
                "brands": [brand for row in rest for brand in row.get("brands", [])],
                "is_target": False,
                "is_jw": False,
                "is_others": True,
                "contribution": rest_contribution,
                "contribution_value": rest_contribution,
                "contribution_pct": (
                    round(100.0 - displayed_pct, 4)
                    if market_growth and displayed_pct is not None and rest_contribution is not None
                    else None
                ),
                "value_recent": rest_value,
            }
        )
        if rest_contribution is None or rest_value is None:
            selected[-1]["data_quality"] = {"available": False, "reason": "no_data"}
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


def _market_share_series(value_series: list[float], total_series: list[float]) -> list[float]:
    return [
        round(value / total * 100, 4) if total else 0.0
        for value, total in zip(value_series, total_series)
    ]


def _period_rank_series_by_brand(rows: list[dict[str, Any]], periods: list[str]) -> dict[str, list[int | None]]:
    """Return each brand's rank for every display period."""

    brand_values: dict[str, list[float]] = defaultdict(lambda: [0.0 for _ in periods])
    brand_complete: dict[str, list[bool]] = defaultdict(lambda: [True for _ in periods])
    for row in rows:
        brand = _row_brand(row)
        if not brand:
            continue
        history = _metric_history(row)
        for idx, period in enumerate(periods):
            value = _optional_value_from_period_item(history.get(period))
            if value is None:
                brand_complete[brand][idx] = False
            else:
                brand_values[brand][idx] += value

    ranks = {brand: [None for _ in periods] for brand in brand_values}
    for idx, _period in enumerate(periods):
        ranked = sorted(
            (
                (brand, values[idx])
                for brand, values in brand_values.items()
                if idx < len(values) and brand_complete[brand][idx] and values[idx] > 0
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        for rank, (brand, _value) in enumerate(ranked, start=1):
            ranks[brand][idx] = rank
    return ranks


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
        rank_series_by_brand = _period_rank_series_by_brand(channel_rows, period_tail)
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
            ms_series = _market_share_series(value_series, total_series)
            ms_recent_pct = ms_series[-1] if ms_series else 0.0
            # 55ff85f4에서 최근 시점 scalar rank만 붙으면서 line chart의
            # 과거 rank가 모두 최근 순위로 보이는 회귀가 생겼다. 멤버십은
            # 최근 top5+기타로 고정해 라인 가독성을 지키되, rank만 해당 기간
            # 전체 브랜드 값을 다시 정렬해 series로 노출한다. 멤버십까지
            # 기간별 재계산하는 대안은 브랜드 라인이 매 기간 바뀌는 churn을
            # 만들어 기각했다.
            rank_series = (
                [None for _ in period_tail]
                if item.get("is_others")
                else rank_series_by_brand.get(str(item.get("brand")), [None for _ in period_tail])
            )
            trend_brands.append(
                {
                    "brand": item.get("brand"),
                    "company": item.get("company"),
                    "is_target": item.get("is_target"),
                    "is_jw": item.get("is_jw"),
                    "is_others": item.get("is_others"),
                    "rank": item.get("rank"),
                    "rank_series": rank_series,
                    "value_series": value_series,
                    "volume_series": value_series,
                    "ms_series": ms_series,
                    "ms_recent_pct": ms_recent_pct,
                    "volume_ms_series": ms_series,
                }
            )
            composition.append(
                {
                    "brand": item.get("brand"),
                    "is_target": item.get("is_target"),
                    "is_jw": item.get("is_jw"),
                    "is_others": item.get("is_others"),
                    "pct": ms_recent_pct,
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


def _analysis_level_market_status_by_channel(
    *,
    level_top5_trend: dict[str, Any],
    analysis_levels: dict[str, Any],
    rows: list[dict[str, Any]],
    source: str,
    channels: list[str] | None,
    include_all_options: bool,
    cache_key: Any | None = None,
) -> dict[str, Any]:
    """Return the deployed portal's chart8-compatible clone-card payload.

    analysis_levels is already built with the desired channel list via
    channels_override. Re-wrapping it into by_level/by_channel creates a new
    contract and breaks the deployed portal, which dereferences
    analysis_level_market_status.data[level].by_channel[channel]. Keep the same
    structure as the production API and let only values differ by regenerated
    data.
    """
    payload = deepcopy(analysis_levels)
    channel_list = [str(channel) for channel in (channels or payload.get("channels") or ["전체"]) if str(channel)]
    if "전체" not in channel_list:
        channel_list = ["전체", *channel_list]
    payload["channels"] = channel_list
    return _ensure_analysis_level_market_status_contract(payload)


def _level_rows_by_segment(rows: list[dict[str, Any]], levels: list[str]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    rows_by_level: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for level in levels:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            for segment_name in _dimension_values(row, level):
                grouped.setdefault(segment_name, []).append(row)
        rows_by_level[level] = grouped
    return rows_by_level


def _level_trend_brand_payloads(
    *,
    option_rows: list[dict[str, Any]],
    periods: list[str],
    target_name: str | None,
    total_series: list[float],
    use_latest_valid_share: bool = False,
) -> list[dict[str, Any]]:
    brand_entries = _display_brand_rows(
        option_rows,
        target_name=target_name,
        top_n=5,
        include_others=True,
    ) if option_rows else []
    row_by_brand = {_row_brand(row): row for row in option_rows if _row_brand(row)}
    rank_series_by_brand = _period_rank_series_by_brand(option_rows, periods)
    selected_series: list[list[float | None]] = []
    brands_in_value = []
    for entry in brand_entries:
        source_row = row_by_brand.get(entry.get("brand"))
        if entry.get("is_others"):
            series = []
            for idx, total in enumerate(total_series):
                selected_values = [item[idx] if idx < len(item) else None for item in selected_series]
                selected_total = _sum_optional_complete(selected_values)
                series.append(
                    round(max(0.0, total - selected_total), 4)
                    if selected_total is not None
                    else None
                )
        else:
            series = (
                _optional_series_for_row(source_row, periods, scaled_sales=True)
                if source_row
                else [None] * len(periods)
            )
            selected_series.append(series)
        # D3도 D2와 같은 회귀를 막는다. 표시 멤버십은 최근 segment top5로
        # 고정하지만, rank_series_10pt는 각 기간의 segment 전체 브랜드 값을
        # 정렬한 순위다. 이렇게 해야 과거 기간 6위/8위였던 고정 멤버도
        # 실제 과거 순위를 tooltip/chart에서 복원할 수 있다.
        rank_series = (
            [None for _ in periods]
            if entry.get("is_others")
            else rank_series_by_brand.get(str(entry.get("brand")), [None for _ in periods])
        )
        recent_value = safe_float(entry.get("value_recent"))
        recent_raw_value = _first_optional_float(entry.get("raw_value"), entry.get("value_recent"))
        recent_share = (
            _latest_valid_share_pct(series, total_series)
            if use_latest_valid_share and recent_value is not None
            else safe_float(entry.get("share_pct"))
        )
        payload = {
            "brand": entry.get("brand"),
            "company": entry.get("company"),
            "is_target": entry.get("is_target"),
            "is_jw": entry.get("is_jw"),
            "is_others": entry.get("is_others"),
            "rank": entry.get("rank"),
            "rank_series_10pt": rank_series,
            "ms_recent_pct": recent_share,
            "value_recent": recent_value,
            "raw_value": recent_raw_value,
            "value_recent_100m": round(recent_value / 100_000_000, 4) if recent_value is not None else None,
            "volume_recent": recent_value,
            "value_series_10pt": series,
            "ms_series_10pt": [
                (
                    round(value / total * 100, 4)
                    if value is not None and total
                    else 0.0
                    if value == 0.0 and total == 0.0
                    else None
                )
                for value, total in zip(series, total_series)
            ],
            "volume_series_10pt": series,
        }
        if recent_value is None:
            payload["data_quality"] = {"available": False, "reason": "no_data"}
        brands_in_value.append(payload)
    return brands_in_value


def _level_top5_trend(
    analysis_levels: dict[str, Any],
    rows: list[dict[str, Any]],
    source: str,
    target_name: str | None,
    *,
    rows_by_level: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
    include_all_options: bool = False,
    channel: str = "전체",
    use_latest_valid_share: bool = False,
    series_value_cache: _SeriesValueCache | None = None,
) -> dict[str, Any]:
    if series_value_cache is None:
        series_value_cache = {}
    levels = analysis_levels.get("levels") or []
    periods = (analysis_levels.get("periods_monthly") or analysis_levels.get("periods_quarterly") or [])[-10:]
    available_levels = [{"key": level, "label": level} for level in levels]
    by_level = {}
    full_market_rows = _rows_for_channel(
        rows,
        source,
        channel,
        periods,
        series_value_cache=series_value_cache,
    )
    full_market_series: list[float] | None = None
    overall_brand_payload_cache: dict[tuple[float | None, ...], list[dict[str, Any]]] = {}
    for level in levels:
        segment_rows_by_name = (
            _rows_for_dimension_segments(
                rows,
                level,
                periods,
                series_value_cache=series_value_cache,
            )
            if channel == "전체"
            else None
        )
        all_level_segments = analysis_levels.get("data", {}).get(level, {}).get("by_channel", {}).get(channel) or []
        overall_segment = next(
            (segment for segment in all_level_segments if isinstance(segment, dict) and segment.get("is_overall")),
            None,
        )
        candidate_segments = [
            segment
            for segment in all_level_segments
            if isinstance(segment, dict) and not segment.get("is_overall") and not _is_excluded_dimension_label(segment.get("name"))
        ]
        level_segments = candidate_segments if include_all_options else candidate_segments[:5]
        values = []
        overall_value_series = (
            list(overall_segment.get("value_series") or [])
            if isinstance(overall_segment, dict)
            else []
        )
        if len(overall_value_series) != len(periods):
            overall_value_series = overall_value_series[-len(periods):] if periods else []
        if overall_segment:
            channel_rows = full_market_rows
            if not overall_value_series:
                overall_value_series = _total_series_for_rows(channel_rows, periods)
            overall_value = safe_float(overall_value_series[-1] if overall_value_series else None)
            overall_series_key = tuple(overall_value_series)
            overall_brands_in_value = overall_brand_payload_cache.get(overall_series_key)
            if overall_brands_in_value is None:
                overall_brands_in_value = _level_trend_brand_payloads(
                    option_rows=channel_rows,
                    periods=periods,
                    # B2: 전체 옵션은 "전체 시장 기준" 뷰다. 여기서는 선택
                    # 브랜드가 top5 밖이어도 들어가야 하므로 target_name을
                    # 전달한다. 전체 옵션을 일반 segment처럼 처리하면 운영의
                    # 전체 시장 기준 chart8과 달라져 기각했다.
                    target_name=target_name,
                    total_series=overall_value_series,
                    use_latest_valid_share=use_latest_valid_share,
                )
                overall_brand_payload_cache[overall_series_key] = overall_brands_in_value
            overall_item = {
                    "value": "전체",
                    "is_default": True,
                    "is_overall": True,
                    "total_value": overall_value,
                    "total_volume": overall_value,
                    "ms_pct": 100.0 if overall_value is not None else None,
                    "brands_in_value": overall_brands_in_value,
                }
            if overall_value is None:
                overall_item["data_quality"] = {"available": False, "reason": "no_data"}
            values.append(overall_item)
        for index, segment in enumerate(level_segments, start=1):
            segment_name = segment.get("name") or f"{level} {index}"
            data_quality = segment.get("data_quality")
            if (
                isinstance(data_quality, dict)
                and data_quality.get("reason") == "dimension_period_missing"
                and periods
                and periods[-1] in (data_quality.get("missing_periods") or [])
            ):
                values.append(
                    {
                        "value": segment_name,
                        "is_default": not values and index == 1,
                        "total_value": None,
                        "total_volume": None,
                        "ms_pct": None,
                        "brands_in_value": [],
                        "data_quality": deepcopy(data_quality),
                    }
                )
                continue
            segment_rows = (
                segment_rows_by_name.get(segment_name, [])
                if segment_rows_by_name is not None
                else _rows_for_dimension(
                    rows,
                    level,
                    segment_name,
                    periods,
                    source=source,
                    channel=channel,
                    series_value_cache=series_value_cache,
                )
            )
            segment_total_series = segment.get("value_series") or _total_series_for_rows(segment_rows, periods)
            if len(segment_total_series) != len(periods):
                segment_total_series = list(segment_total_series)[-len(periods):] if periods else []
            total_value = safe_float(segment_total_series[-1] if segment_total_series else None)
            segment_item = {
                    "value": segment_name,
                    "is_default": not values and index == 1,
                    "total_value": total_value,
                    "total_volume": total_value,
                    "ms_pct": safe_float(segment.get("recent_share_pct")),
                    "brands_in_value": _level_trend_brand_payloads(
                        option_rows=segment_rows,
                        periods=periods,
                        # B2: 개별 segment 옵션은 "그 분류 안의 top5+기타"가
                        # 계약이다. 선택 브랜드 강제 포함은 다른 class/molecule에
                        # 속한 타깃을 0으로 끼워 넣는 위장을 만들 수 있어
                        # target_name을 비운다.
                        target_name=None,
                        total_series=segment_total_series,
                        use_latest_valid_share=use_latest_valid_share,
                    ),
                }
            if total_value is None:
                segment_item["data_quality"] = {"available": False, "reason": "no_data"}
            values.append(segment_item)
        if not overall_value_series and full_market_series is None and periods:
            full_market_series = _total_series_for_rows(full_market_rows, periods)
        overall_total = safe_float(
            overall_value_series[-1]
            if overall_value_series
            else full_market_series[-1]
            if full_market_series
            else None
        )
        fallback_total = _sum_optional_complete(item.get("total_value") for item in values)
        by_level[level] = {
            "level_label": level,
            "level_value": values[0]["value"] if values else None,
            "default_value": values[0]["value"] if values else None,
            "total_market_value": overall_total if overall_total is not None else fallback_total,
            "empty": not bool(values),
            "periods_10pt": periods,
            "all_options": [value.get("value") for value in values if value.get("value")],
            "default_option": values[0]["value"] if values else None,
            "values": values,
        }
    return {
        "available_levels": available_levels,
        "default_level": levels[0] if levels else None,
        "by_level": by_level,
        "note": "각 분석 level top 5 + 기타",
    }


def _catalog_member_rows(strategic_brand: Any, view_source_id: str) -> list[dict[str, Any]]:
    if strategic_brand is None:
        return []
    if isinstance(strategic_brand, list):
        if view_source_id.startswith("ml_"):
            return [dict(row) for row in strategic_brand if str(row.get("ml_id") or "") == view_source_id]
        if view_source_id.startswith("cd_"):
            return [dict(row) for row in strategic_brand if str(row.get("cd_id") or "") == view_source_id]
        return []
    if view_source_id.startswith("ml_"):
        sub = strategic_brand[strategic_brand["ml_id"].astype(str) == view_source_id]
    elif view_source_id.startswith("cd_") and "cd_id" in strategic_brand.columns:
        sub = strategic_brand[strategic_brand["cd_id"].astype(str) == view_source_id]
    else:
        return []
    return [row.to_dict() for _, row in sub.iterrows()]


def _catalog_members_for_market(strategic_brand: Any, view_source_id: str) -> list[dict[str, Any]]:
    field = "cd_id" if view_source_id.startswith("cd_") else "ml_id"
    rows = active_catalog_member_rows(strategic_brand, field, view_source_id)
    members = []
    seen: set[str] = set()
    for row in rows:
        name = str(row.get("canonical_name") or row.get("name") or "")
        if name and name not in seen:
            seen.add(name)
            members.append({"name": name, "is_jw": bool(row.get("is_jw")), "company": row.get("판매사")})
    return members


def market_size_series_with_yoy(series: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    yoy_series = market_yoy_series(series)
    mom_series = market_cmgr_series(series)
    output: dict[str, dict[str, float | None]] = {}
    if not isinstance(series, dict):
        return output
    for period in sorted(series.keys(), key=period_key):
        value = safe_float(series.get(period))
        output[str(period)] = {
            "value": value,
            "yoy_growth_pct": yoy_series.get(str(period)),
            "mom_growth_pct": mom_series.get(str(period)),
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


def market_cmgr_series(series: dict[str, Any]) -> dict[str, float | None]:
    if not isinstance(series, dict):
        return {}
    periods = sorted(series.keys(), key=period_key)
    values = {str(period): safe_float(series.get(period)) for period in periods}
    growth_by_period = fixed_five_year_growth_series(values)
    result: dict[str, float | None] = {}
    for period in periods:
        growth = growth_by_period[str(period)].value
        result[str(period)] = round(growth, 4) if growth is not None else None
    return result


def _prior_year_period(period: str) -> str:
    year, suffix = period.split("-", 1)
    return f"{int(year) - 1}-{suffix}"


def top3_share(rows: list[dict[str, Any]]) -> float | None:
    shares = []
    for row in rows:
        recent = metric_recent(_metric_history(row))
        shares.append(safe_float(recent.get("ms")))
    if not shares:
        return None
    return round(sum(sorted(shares, reverse=True)[:3]), 2)


def atc_codes_from_market_catalog(market_catalog_row: dict[str, Any] | None) -> list[str]:
    raw_codes = decode_json((market_catalog_row or {}).get("atc_codes_json"))
    if not isinstance(raw_codes, list):
        return []
    return [str(code).strip() for code in raw_codes if str(code).strip()]


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
    analysis_profile_sig: str = "",
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
    analysis_cache_key = (analysis_view_id, source_api, measure, analysis_profile_sig)
    ubist_channel_context = None
    if source_api == "UBIST":
        with strategic_channel_totals_context(sibling_rows):
            ubist_channel_context = resolve_market_channels(rows=sibling_rows, market=market_catalog_row, measure=measure)
    channels_override = (
        ubist_channel_context.get("channels")
        if isinstance(ubist_channel_context, dict) and ubist_channel_context.get("channels")
        else None
    )
    include_all_d3_options = bool(brand_row.get("is_jw") or brand_row.get("is_target"))
    analysis_series_value_cache: _SeriesValueCache = {}
    analysis_series_observed_cache: _SeriesObservedCache = {}
    block_epoch = current_analysis_level_source_epoch()
    precomputed_block = (
        load_analysis_level_block(
            key=AnalysisLevelBlockKey(
                view="strategic_cd" if view_type == "competitive_dynamics" else "strategic_ml",
                market_id=view_source_id,
                source=source_api,
                measure=measure,
                profile_sig=analysis_profile_sig,
                trim_mode="full" if include_all_d3_options else "trim",
            ),
            source_epoch=block_epoch,
        )
        if block_epoch is not None
        else None
    )
    if precomputed_block is not None:
        analysis_levels = deepcopy(precomputed_block.analysis_levels)
        ANALYSIS_LEVELS_CACHE[analysis_cache_key] = deepcopy(analysis_levels)
    elif analysis_cache_key not in ANALYSIS_LEVELS_CACHE:
        resolved_levels = set(_strategic_levels(market_catalog_row, sibling_rows))
        resolved_periods = _history_periods(sibling_rows, source_api)
        ANALYSIS_LEVELS_CACHE[analysis_cache_key] = _build_analysis_levels_from_mart(
            rows=sibling_rows,
            source=source_api,
            market=market_catalog_row,
            view_source_id=analysis_view_id,
            target_name=None,
            fallback_level_top5=level_top5,
            channels_override=channels_override,
            resolved_levels=resolved_levels,
            resolved_periods=resolved_periods,
            series_value_cache=analysis_series_value_cache,
            series_observed_cache=analysis_series_observed_cache,
        )
    else:
        resolved_levels = None
        resolved_periods = None
    if precomputed_block is None:
        analysis_levels = _ensure_split_class_alias(deepcopy(ANALYSIS_LEVELS_CACHE[analysis_cache_key]))
    if analysis_cache_key not in LEVEL_ROW_GROUPS_CACHE:
        LEVEL_ROW_GROUPS_CACHE[analysis_cache_key] = _level_rows_by_segment(
            sibling_rows,
            ANALYSIS_LEVELS_CACHE[analysis_cache_key].get("levels") or [],
        )
    if precomputed_block is None and not include_all_d3_options:
        analysis_levels = _trim_analysis_levels(analysis_levels)
    annual_rank_cache: _AnnualRankRowsCache = {}
    brand_ranking_stacked = _stacked_ranking(
        brand_ranking,
        label_key="brand",
        target_name=brand_row.get("brand_name"),
        catalog_members=catalog_members,
        full_rows=sibling_rows,
        annual_rank_cache=annual_rank_cache,
        target_overrides=_target_rank_overrides(
            sibling_rows,
            label_key="brand",
            target_name=brand_row.get("brand_name"),
            cache_key=(view_source_id, source_api, measure),
            annual_rank_cache=annual_rank_cache,
        ),
    )
    company_ranking_stacked = _stacked_ranking(
        company_ranking,
        label_key="company",
        target_name=target_company_name,
        full_rows=sibling_rows,
        annual_rank_cache=annual_rank_cache,
        target_overrides=_target_rank_overrides(
            sibling_rows,
            label_key="company",
            target_name=target_company_name,
            cache_key=("company", view_source_id, source_api, measure),
            annual_rank_cache=annual_rank_cache,
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
    target_display = next((row for row in display_entries_no_others if row.get("is_target")), {})
    periods = resolved_periods if resolved_periods is not None else _history_periods(sibling_rows, source_api)
    hhi_points = _annual_share_hhi_from_rows(
        sibling_rows,
        label_key="brand",
        source=source_api,
        annual_rank_cache=annual_rank_cache,
    )
    if hhi_points:
        hhi_series = hhi_points
        hhi_recent = safe_float(hhi_points[-1].get("hhi"))
    company_concentration = _company_hhi_from_rows(
        sibling_rows,
        source=source_api,
        annual_rank_cache=annual_rank_cache,
    )
    data_period_coverage = _data_period_coverage(market_series, source=source_api)
    growth_contribution = _growth_contribution_payload(sibling_rows, brand_row.get("brand_name"), periods, source=source_api)
    target_customer_channels = analysis_levels.get("channels")
    if source_api == "UBIST":
        specialty_channels = ubist_channel_context.get("specialty_channels")
        if isinstance(specialty_channels, list) and specialty_channels:
            target_customer_channels = [str(channel) for channel in specialty_channels]
    analysis_level_market_channels = target_customer_channels or _channels_for_source(source_api)
    clone_analysis_levels = analysis_levels
    if analysis_level_market_channels and precomputed_block is None:
        clone_levels_key = (analysis_cache_key, "analysis_level_market_status", tuple(analysis_level_market_channels))
        if clone_levels_key not in ANALYSIS_LEVELS_BY_CHANNEL_CACHE:
            if resolved_levels is None:
                resolved_levels = set(_strategic_levels(market_catalog_row, sibling_rows))
            if resolved_periods is None:
                resolved_periods = _history_periods(sibling_rows, source_api)
            ANALYSIS_LEVELS_BY_CHANNEL_CACHE[clone_levels_key] = _build_analysis_levels_from_mart(
                rows=sibling_rows,
                source=source_api,
                market=market_catalog_row,
                view_source_id=analysis_view_id,
                target_name=None,
                fallback_level_top5=level_top5,
                channels_override=analysis_level_market_channels,
                resolved_levels=resolved_levels,
                resolved_periods=resolved_periods,
                series_value_cache=analysis_series_value_cache,
                series_observed_cache=analysis_series_observed_cache,
            )
        clone_analysis_levels = _ensure_split_class_alias(deepcopy(ANALYSIS_LEVELS_BY_CHANNEL_CACHE[clone_levels_key]))
        if not include_all_d3_options:
            clone_analysis_levels = _trim_analysis_levels(clone_analysis_levels)

    target_customer_competition_by_channel = _target_customer_competition(
        rows=sibling_rows,
        source=source_api,
        target_name=brand_row.get("brand_name"),
        periods=periods,
        channels=target_customer_channels,
    )
    level_top5_trend = _level_top5_trend(
        analysis_levels,
        sibling_rows,
        source_api,
        brand_row.get("brand_name"),
        rows_by_level=LEVEL_ROW_GROUPS_CACHE[analysis_cache_key],
        include_all_options=include_all_d3_options,
    )
    target_customer_competition = target_customer_competition_by_channel
    if precomputed_block is not None:
        analysis_level_market_status = deepcopy(precomputed_block.analysis_level_market_status)
    else:
        analysis_level_market_status = _ensure_analysis_level_market_status_contract(_analysis_level_market_status_by_channel(
            level_top5_trend=level_top5_trend,
            analysis_levels=clone_analysis_levels,
            rows=sibling_rows,
            source=source_api,
            channels=analysis_level_market_channels,
            include_all_options=include_all_d3_options,
            cache_key=(analysis_cache_key, include_all_d3_options, tuple(analysis_level_market_channels or [])),
        ))
    direct_competition_count = max(
        len({r.get("brand_key") for r in sibling_rows if r.get("brand_key")}),
        len({member["name"] for member in catalog_members if member.get("name")}),
    )
    market_series_payload = latest_market_series_payload(market_series)
    # 표시 CAGR도 EI 계산에 쓰인 endpoint CAGR과 같은 basis를 쓴다.
    # annual-partial 제거 후에도 brand 5년 시작값이 0이면 EI는 3년으로
    # fallback되므로, market 5년값을 따로 표시하면 다시 두 경로가 갈라진다.
    # 순수 market 5년 표시만 고집하는 대안은 PL의 "표시 CAGR == EI CAGR"
    # 검증 게이트와 어긋나 기각했다.
    display_market_cagr = optional_float(target_display.get("market_cagr_pct"))
    if display_market_cagr is None:
        display_market_cagr = series_cagr(market_series)

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
                "market_cagr_5y_pct": display_market_cagr,
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
                **market_series_payload,
                "periods_unit": "월간" if brand_row["source"] == "ubist" else "분기",
                "hhi_series_5y": hhi_points,
                "hhi_recent": hhi_recent,
                "cagr_5y_pct": display_market_cagr,
            },
            "market_size_series": market_series_payload["market_size_series"],
            "market_yoy_series": market_series_payload["market_yoy_series"],
            "market_yoy_recent_pct": market_series_payload["market_yoy_recent_pct"],
            "hhi_series_5y": hhi_series,
            "hhi_recent": hhi_recent,
            "brand_ranking": brand_ranking_stacked,
            "company_ranking": company_ranking_stacked,
            "ei_ms_matrix": _matrix_payload(display_entries_no_others),
            "growth_contribution_ms_matrix": _matrix_payload(display_entries_no_others),
            "growth_contribution": growth_contribution,
            "level_top5_trend": level_top5_trend,
            "target_customer_competition": target_customer_competition,
            "target_customer_competition_by_channel": target_customer_competition_by_channel,
            "analysis_level_market_status": analysis_level_market_status,
            "ubist_specialty_channels": (
                ubist_channel_context.get("specialty_channels")
                if isinstance(ubist_channel_context, dict)
                else None
            ),
            "ubist_specialty_target_channels": (
                ubist_channel_context.get("specialty_target_channels")
                if isinstance(ubist_channel_context, dict)
                else None
            ),
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
            "atc_codes": atc_codes_from_market_catalog(market_catalog_row),
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
    args = parse_args()
    strategic_brand = load_catalog("strategic_brand")
    ml_market = load_catalog("ml_market").set_index("ml_id", drop=False)
    cd_market = load_catalog("cd_market").rename(columns={"cd_id": "cd_market_id"}).set_index("cd_market_id", drop=False)

    selected_ml_ids: list[str] | None = None
    selected_cd_ids: list[str] | None = None
    selected_market_ids: list[str] | None = None
    if args.market:
        selected_ml = str(args.market)
        if selected_ml not in ml_market.index:
            raise SystemExit(f"--market not found in ml_market catalog: {selected_ml}")
        selected_ml_ids = [selected_ml]
        selected_cd_ids = [
            str(row["cd_market_id"])
            for _, row in cd_market.loc[cd_market["ml_id"].astype(str) == selected_ml].iterrows()
        ]
        selected_market_ids = [ml_to_strategy(selected_ml)]
        print(f"[B3] partial cache_cause regeneration: ml={selected_ml}, cd={selected_cd_ids}", flush=True)

    ml_market_sql, ml_market_params = selected_query("mart_strategic_ml_market_metric", "ml_id", selected_ml_ids)
    cd_market_sql, cd_market_params = selected_query("mart_strategic_cd_market_metric", "cd_market_id", selected_cd_ids)
    ml_brand_sql, ml_brand_params = selected_query("mart_strategic_ml_brand_metric", "ml_id", selected_ml_ids)
    cd_brand_sql, cd_brand_params = selected_query("mart_strategic_cd_brand_metric", "cd_market_id", selected_cd_ids)

    ml_market_rows = {
        (r["ml_id"], r["source"], r["measure"]): r for r in fetch_all(ml_market_sql, ml_market_params)
    }
    cd_market_rows = {
        (r["cd_market_id"], r["source"], r["measure"]): r for r in fetch_all(cd_market_sql, cd_market_params)
    }
    ml_brand_rows = fetch_all(ml_brand_sql, ml_brand_params)
    cd_brand_rows = fetch_all(cd_brand_sql, cd_brand_params)
    ml_siblings = make_sibling_map(ml_brand_rows, "ml_id")
    cd_siblings = make_sibling_map(cd_brand_rows, "cd_market_id")

    columns = ["brand", "view_type", "source", "measure", "market_id", "response_json", "payload_size"]
    placeholders = ", ".join(["%s"] * len(columns))
    names = ", ".join(f"`{c}`" for c in columns)
    inserted = 0
    conn = mariadb_connect()
    cur = conn.cursor()
    batch: list[tuple[Any, ...]] = []
    partial_mode = bool(args.market)
    should_switch_full = False
    if partial_mode:
        conn.autocommit(False)
        conn.begin()

    try:
        serving_brand_names = load_serving_brand_names(cur) if not args.full_all_brands else set()
        ml_output_rows = filter_serving_brand_rows(
            ml_brand_rows,
            serving_brand_names,
            full_all_brands=args.full_all_brands,
        )
        cd_output_rows = filter_serving_brand_rows(
            cd_brand_rows,
            serving_brand_names,
            full_all_brands=args.full_all_brands,
        )
        if args.verbose:
            mode = "full-all-brands" if args.full_all_brands else "serving-slim"
            print(
                f"[B3] cache_cause output mode={mode} "
                f"ml_rows={len(ml_output_rows)}/{len(ml_brand_rows)} "
                f"cd_rows={len(cd_output_rows)}/{len(cd_brand_rows)}",
                flush=True,
            )

        target_table_name = args.target_table
        if not partial_mode:
            target_table_name, should_switch_full = prepare_full_target_table(cur, args.target_table)
        target_table = _quoted_table_name(target_table_name)
        sql = f"REPLACE INTO {target_table} ({names}) VALUES ({placeholders})"

        if selected_market_ids is not None:
            placeholders = ",".join(["%s"] * len(selected_market_ids))
            cur.execute(
                f"DELETE FROM {target_table} "
                f"WHERE market_id IN ({placeholders}) "
                "AND view_type IN ('market_landscape','competitive_dynamics')",
                tuple(selected_market_ids),
            )
            print(f"[B3] partial DELETE {args.target_table} market_id={selected_market_ids}", flush=True)
        else:
            cur.execute(f"DELETE FROM {target_table}")

        def flush_batch() -> None:
            nonlocal batch
            if not batch:
                return
            cur.executemany(sql, batch)
            batch = []

        for row in ml_output_rows:
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

        for row in cd_output_rows:
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
        if partial_mode:
            conn.commit()
            print(f"[B3] partial commit cache_cause rows={inserted}", flush=True)
        elif should_switch_full:
            old_table = switch_full_cache_cause(cur, target_table_name)
            conn.commit()
            print(f"[B3] full cache_cause blue-green switch rows={inserted} old_table={old_table}", flush=True)
        else:
            conn.commit()
    except Exception:
        if partial_mode:
            conn.rollback()
        elif not conn.get_autocommit():
            conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
    if args.verbose:
        print(f"cache_cause rows={inserted} ml_rows={len(ml_brand_rows)} cd_rows={len(cd_brand_rows)}")


if __name__ == "__main__":
    main()
