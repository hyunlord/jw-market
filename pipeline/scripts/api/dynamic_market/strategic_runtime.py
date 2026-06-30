"""Runtime strategic dynamic-market payload builder.

This path intentionally reuses the cache-cause strategic overlay builder so
dynamic strategic responses keep the same payload contract as `/api/cause`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import lru_cache
import json
from pathlib import Path
import sys
from threading import RLock
from typing import Any

from pipeline.etl.io.mart.brand_key_normalize import normalize_brand_name
from pipeline.scripts.api import db
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.dynamic_market.resolvers import normalize_source
from pipeline.scripts.api.dynamic_market.types import DynamicMarketInputError, quote_identifier
from pipeline.scripts.api.models.dynamic_market import DynamicMarketAnalysisLevelFilters


ETL_DIR = Path(__file__).resolve().parents[2] / "etl"
if str(ETL_DIR) not in sys.path:
    sys.path.insert(0, str(ETL_DIR))

from pipeline.scripts.etl import build_cache_cause as cause_builder  # noqa: E402


JsonRow = dict[str, Any]
_CAUSE_BUILDER_LOCK = RLock()


def build_strategic_payload(
    *,
    mart_db: str,
    ml_id: str | None,
    cd_market_id: str | None,
    focus_brand_key: str | None,
    source: str,
    measure: str,
    analysis_level: DynamicMarketAnalysisLevelFilters,
) -> JsonRow:
    """Build a strategic dynamic-market response with cache-cause overlays."""

    market_kind, view_source_id = _resolve_market_id(ml_id=ml_id, cd_market_id=cd_market_id)
    mart_source = normalize_source(source)
    source_api = cause_builder.api_source(mart_source)
    brand_table, market_table, id_column = _tables_for_market_kind(market_kind)
    sibling_rows = _fetch_sibling_rows(
        mart_db=mart_db,
        table=brand_table,
        id_column=id_column,
        market_id=view_source_id,
        source=mart_source,
        measure=measure,
    )
    if not sibling_rows:
        raise DynamicMarketInputError(
            f"strategic market rows were not found: market_id={view_source_id}, source={mart_source}, measure={measure}"
        )

    filtered_rows = _filter_rows_by_analysis_level(
        rows=sibling_rows,
        source=mart_source,
        analysis_level=analysis_level,
    )
    if not filtered_rows:
        raise DynamicMarketInputError("analysis-level filters removed all strategic market rows")

    brand_row = _choose_focus_row(filtered_rows, focus_brand_key)
    market_row = _fetch_market_row(
        mart_db=mart_db,
        table=market_table,
        id_column=id_column,
        market_id=view_source_id,
        source=mart_source,
        measure=measure,
    )
    if not market_row:
        raise DynamicMarketInputError(
            "strategic market total row was not found: "
            f"market_id={view_source_id}, source={mart_source}, measure={measure}"
        )
    has_runtime_filter = len(filtered_rows) != len(sibling_rows)
    if has_runtime_filter:
        market_row = _market_row_for_filtered_rows(market_row, filtered_rows)

    market_catalog_row = _catalog_row(market_kind, view_source_id)
    strategic_brand = _strategic_brand_catalog()
    if has_runtime_filter:
        _clear_cause_builder_runtime_caches()
    with _CAUSE_BUILDER_LOCK:
        original_resolver = cause_builder.resolve_market_channels
        cause_builder.resolve_market_channels = _runtime_resolve_market_channels(original_resolver)
        try:
            raw_payload = cause_builder.build_response(
                brand_row=brand_row,
                market_row=market_row,
                sibling_rows=filtered_rows,
                view_type=_view_type(market_kind),
                market_id=_response_market_id(market_kind, view_source_id),
                source=source_api,
                measure=measure,
                view_source_id=view_source_id,
                market_name=_market_name(market_row, market_catalog_row),
                market_sources=_market_sources(market_catalog_row, source_api),
                market_catalog_row=market_catalog_row,
                strategic_brand=strategic_brand,
            )
        finally:
            cause_builder.resolve_market_channels = original_resolver
    composed = compose_cached_json(raw_payload, measure=measure)
    if not isinstance(composed, dict):
        raise DynamicMarketInputError("strategic payload composition did not return an object")
    return composed


def _resolve_market_id(*, ml_id: str | None, cd_market_id: str | None) -> tuple[str, str]:
    if cd_market_id:
        normalized = cd_market_id.strip()
        if not normalized.startswith("cd_"):
            raise DynamicMarketInputError(f"unsupported competitive-dynamics market id: {cd_market_id}")
        return "cd", normalized
    if ml_id:
        normalized = ml_id.strip()
        if normalized.startswith("strategy_"):
            normalized = f"ml_{int(normalized.removeprefix('strategy_')):03d}"
        if not normalized.startswith("ml_"):
            raise DynamicMarketInputError(f"unsupported market-landscape market id: {ml_id}")
        return "ml", normalized
    raise DynamicMarketInputError("strategic dynamic-market requests require ml_id or cd_market_id")


def _tables_for_market_kind(market_kind: str) -> tuple[str, str, str]:
    if market_kind == "cd":
        return "mart_strategic_cd_brand_metric", "mart_strategic_cd_market_metric", "cd_market_id"
    return "mart_strategic_ml_brand_metric", "mart_strategic_ml_market_metric", "ml_id"


def _fetch_sibling_rows(
    *,
    mart_db: str,
    table: str,
    id_column: str,
    market_id: str,
    source: str,
    measure: str,
) -> list[JsonRow]:
    return db.fetch_all(
        f"""
        SELECT *
        FROM {quote_identifier(mart_db)}.{table}
        WHERE {id_column} = %s
          AND source = %s
          AND measure = %s
        ORDER BY brand_name, brand_key
        """,
        [market_id, source, measure],
    )


def _fetch_market_row(
    *,
    mart_db: str,
    table: str,
    id_column: str,
    market_id: str,
    source: str,
    measure: str,
) -> JsonRow | None:
    return db.fetch_one(
        f"""
        SELECT *
        FROM {quote_identifier(mart_db)}.{table}
        WHERE {id_column} = %s
          AND source = %s
          AND measure = %s
        LIMIT 1
        """,
        [market_id, source, measure],
    )


def _choose_focus_row(rows: Sequence[JsonRow], focus_brand_key: str | None) -> JsonRow:
    if focus_brand_key:
        requested = focus_brand_key.strip()
        requested_key = normalize_brand_name(requested)
        for row in rows:
            brand_key = str(row.get("brand_key") or "").strip()
            brand_name = str(row.get("brand_name") or "").strip()
            if requested in {brand_key, brand_name} or requested_key in {brand_key, normalize_brand_name(brand_name)}:
                return dict(row)
    for row in rows:
        if bool(row.get("is_target")):
            return dict(row)
    for row in rows:
        if bool(row.get("is_jw")):
            return dict(row)
    return dict(rows[0])


def _filter_rows_by_analysis_level(
    *,
    rows: Sequence[JsonRow],
    source: str,
    analysis_level: DynamicMarketAnalysisLevelFilters,
) -> list[JsonRow]:
    selected = _selected_filters(source=source, analysis_level=analysis_level)
    if not selected:
        return [dict(row) for row in rows]
    filtered: list[JsonRow] = []
    for row in rows:
        dimensions = _decode_object(row.get("by_dimension"))
        if all(_row_matches_dimension(dimensions, key, values) for key, values in selected.items()):
            filtered.append(dict(row))
    return filtered


def _selected_filters(*, source: str, analysis_level: DynamicMarketAnalysisLevelFilters) -> dict[str, tuple[str, ...]]:
    source_filters = analysis_level.ubist if source == "ubist" else analysis_level.iqvia
    selected: dict[str, tuple[str, ...]] = {}
    for key, values in source_filters.model_dump().items():
        clean = tuple(str(value).strip() for value in values if str(value).strip())
        if clean:
            selected[key] = clean
    return selected


def _row_matches_dimension(dimensions: Mapping[str, Any], key: str, selected_values: Sequence[str]) -> bool:
    candidates = _dimension_candidates(dimensions, key)
    if not candidates:
        return False
    normalized_selected = {normalize_brand_name(value) or value.strip().lower() for value in selected_values}
    for candidate in candidates:
        text = str(candidate).strip()
        if text in selected_values or (normalize_brand_name(text) or text.lower()) in normalized_selected:
            return True
    return False


def _dimension_candidates(dimensions: Mapping[str, Any], key: str) -> tuple[Any, ...]:
    aliases = {
        "seller": ("seller", "mfr", "manufacturer", "company_name"),
        "mfr_name_kor": ("mfr_name_kor", "mfr", "manufacturer", "company_name"),
        "molecule": ("molecule", "molecule_desc"),
        "molecule_strength": ("molecule_strength", "strength_pack", "성분용량"),
        "form": ("form", "dosage_form", "제형"),
        "route": ("route", "투여경로"),
        "reimbursement": ("reimbursement", "nhi_type", "nhi", "급여구분"),
        "atc3": ("atc3", "atc3_code"),
        "atc4": ("atc4", "atc4_code"),
    }
    values: list[Any] = []
    for alias in aliases.get(key, (key,)):
        value = dimensions.get(alias)
        if isinstance(value, list):
            values.extend(value)
        elif value not in (None, ""):
            values.append(value)
    return tuple(values)


def _market_row_for_filtered_rows(market_row: JsonRow, rows: Sequence[JsonRow]) -> JsonRow:
    market_series: dict[str, float] = {}
    for row in rows:
        for period, value in _history_values(row).items():
            market_series[period] = market_series.get(period, 0.0) + value
    filtered = dict(market_row)
    filtered["market_size_series"] = json.dumps(dict(sorted(market_series.items())), ensure_ascii=False)
    filtered["brand_ranking_stacked"] = None
    filtered["company_ranking_stacked"] = None
    filtered["hhi_series_5y"] = None
    return filtered


def _history_values(row: Mapping[str, Any]) -> dict[str, float]:
    history = _decode_object(row.get("raw_value_history")) or _decode_object(row.get("metric_history"))
    values: dict[str, float] = {}
    for period, item in history.items():
        if isinstance(item, Mapping):
            raw = item.get("raw_value", item.get("value", item.get("market_size")))
        else:
            raw = item
        try:
            values[str(period)] = float(raw or 0.0)
        except (TypeError, ValueError):
            values[str(period)] = 0.0
    return values


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


def _runtime_resolve_market_channels(original_resolver: Any) -> Any:
    def resolve(*, rows: list[JsonRow], market: JsonRow | None, measure: str, max_channels: int = 4) -> JsonRow:
        resolved = original_resolver(rows=rows, market=market, measure=measure, max_channels=max_channels)
        specialty_channels = resolved.get("specialty_channels") if isinstance(resolved, dict) else None
        if isinstance(specialty_channels, list) and len(specialty_channels) > 1:
            return resolved
        fallback = _specialty_channels_from_mart_rows(rows, max_channels=max_channels)
        return fallback or resolved

    return resolve


def _specialty_channels_from_mart_rows(rows: list[JsonRow], *, max_channels: int) -> JsonRow | None:
    totals: dict[str, float] = {}
    per_row_series: list[tuple[JsonRow, dict[str, Any]]] = []
    for row in rows:
        specialty_data = _decode_object(row.get("specialty_data"))
        if not specialty_data:
            continue
        per_row_series.append((row, specialty_data))
        for channel, series in specialty_data.items():
            channel_text = str(channel).strip()
            if not channel_text or channel_text == "전체":
                continue
            if channel_text.lower().startswith("others("):
                continue
            if isinstance(series, dict):
                totals[channel_text] = totals.get(channel_text, 0.0) + sum(
                    _history_item_value(item) for item in series.values()
                )
    selected = [channel for channel, _ in sorted(totals.items(), key=lambda item: item[1], reverse=True)[:max_channels]]
    if not selected:
        return None
    for row, specialty_data in per_row_series:
        selected_series = {channel: specialty_data.get(channel, {}) for channel in selected}
        row["__ubist_dual_channel_data"] = selected_series
        row["__ubist_specialty_channel_data"] = selected_series
    target_channels = [{"code": channel, "display_name": channel} for channel in selected]
    return {
        "channels": ["전체", "상급종병", "종병", "병원", "의원", "보건소", "기타"],
        "specialty_channels": ["전체", *selected],
        "target_channels": target_channels,
        "specialty_target_channels": target_channels,
        "fallback_codes": selected,
        "series_brand_count": len(per_row_series),
        "raw_brand_count": len(rows),
        "fallback_source": "mart_specialty_data",
    }


def _history_item_value(item: Any) -> float:
    raw = item.get("raw_value", item.get("value", 0.0)) if isinstance(item, Mapping) else item
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


@lru_cache(maxsize=4)
def _ml_market_catalog() -> Mapping[str, JsonRow]:
    try:
        frame = cause_builder.load_catalog("ml_market")
    except Exception:
        return {}
    return {str(row["ml_id"]): row.to_dict() for _, row in frame.iterrows() if row.get("ml_id") is not None}


@lru_cache(maxsize=4)
def _cd_market_catalog() -> Mapping[str, JsonRow]:
    try:
        frame = cause_builder.load_catalog("cd_market").rename(columns={"cd_id": "cd_market_id"})
    except Exception:
        return {}
    return {
        str(row["cd_market_id"]): row.to_dict()
        for _, row in frame.iterrows()
        if row.get("cd_market_id") is not None
    }


@lru_cache(maxsize=2)
def _strategic_brand_catalog() -> Any:
    try:
        return cause_builder.load_catalog("strategic_brand")
    except Exception:
        return None


def _catalog_row(market_kind: str, view_source_id: str) -> JsonRow:
    catalog = _cd_market_catalog() if market_kind == "cd" else _ml_market_catalog()
    return dict(catalog.get(view_source_id, {}))


def _view_type(market_kind: str) -> str:
    return "competitive_dynamics" if market_kind == "cd" else "market_landscape"


def _response_market_id(market_kind: str, view_source_id: str) -> str:
    if market_kind == "cd":
        return str(cause_builder.ml_to_strategy(_catalog_row("cd", view_source_id).get("ml_id") or view_source_id))
    return str(cause_builder.ml_to_strategy(view_source_id))


def _market_name(market_row: Mapping[str, Any], market_catalog_row: Mapping[str, Any]) -> str | None:
    for key in ("name", "market_name", "ml_name", "cd_name"):
        value = market_catalog_row.get(key) or market_row.get(key)
        if value:
            return str(value)
    return None


def _market_sources(market_catalog_row: Mapping[str, Any], source_api: str) -> list[str]:
    sources = cause_builder.source_list(market_catalog_row.get("data_source"))
    return sources or [source_api]


def _clear_cause_builder_runtime_caches() -> None:
    for cache_name in (
        "ANALYSIS_LEVELS_CACHE",
        "LEVEL_ROW_GROUPS_CACHE",
        "ANALYSIS_LEVELS_BY_CHANNEL_CACHE",
        "ANALYSIS_LEVEL_STATUS_CHANNEL_CACHE",
        "EI_META_CACHE",
        "TARGET_RANK_STATS_CACHE",
    ):
        cache = getattr(cause_builder, cache_name, None)
        if hasattr(cache, "clear"):
            cache.clear()
