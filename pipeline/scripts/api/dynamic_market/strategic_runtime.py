"""Runtime strategic dynamic-market payload builder.

This path intentionally reuses the cache-cause strategic overlay builder so
dynamic strategic responses keep the same payload contract as `/api/cause`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import sys
from threading import RLock
from typing import Any

from pipeline.etl.io.mart.brand_key_normalize import normalize_brand_name
from pipeline.scripts.api import db
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.dynamic_market.resolvers import normalize_source
from pipeline.scripts.api.dynamic_market.strategic_runtime_catalog import (
    catalog_row,
    clear_runtime_caches,
    market_sources,
    response_market_id,
    strategic_brand_catalog,
)
from pipeline.scripts.api.dynamic_market.strategic_runtime_channels import (
    runtime_resolve_market_channels as _runtime_resolve_market_channels,
)
from pipeline.scripts.api.dynamic_market.strategic_runtime_cache import build_cached_payload
from pipeline.scripts.api.dynamic_market.strategic_runtime_filters import (
    filter_rows_by_analysis_level,
    market_row_for_filtered_rows,
)
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

    return build_cached_payload(
        builder=_build_strategic_payload_uncached,
        mart_db=mart_db,
        ml_id=ml_id,
        cd_market_id=cd_market_id,
        focus_brand_key=focus_brand_key,
        source=source,
        measure=measure,
        analysis_level=analysis_level,
    )


def _build_strategic_payload_uncached(
    *,
    mart_db: str,
    ml_id: str | None,
    cd_market_id: str | None,
    focus_brand_key: str | None,
    source: str,
    measure: str,
    analysis_level: DynamicMarketAnalysisLevelFilters,
) -> JsonRow:
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

    filtered_rows = filter_rows_by_analysis_level(
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
        market_row = market_row_for_filtered_rows(market_row, filtered_rows)

    market_catalog_row = _catalog_row(market_kind, view_source_id)
    strategic_brand = strategic_brand_catalog(cause_builder)
    if has_runtime_filter:
        clear_runtime_caches(cause_builder)
    with _CAUSE_BUILDER_LOCK:
        original_resolver = cause_builder.resolve_market_channels
        cause_builder.resolve_market_channels = _runtime_resolve_market_channels(original_resolver)
        try:
            raw_payload = cause_builder.build_response(
                brand_row=brand_row,
                market_row=market_row,
                sibling_rows=filtered_rows,
                view_type=_view_type(market_kind),
                market_id=response_market_id(cause_builder, market_kind, view_source_id),
                source=source_api,
                measure=measure,
                view_source_id=view_source_id,
                market_name=_market_name(market_row, market_catalog_row),
                market_sources=market_sources(cause_builder, market_catalog_row, source_api),
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


def _catalog_row(market_kind: str, view_source_id: str) -> JsonRow:
    return catalog_row(cause_builder, market_kind, view_source_id)


def _view_type(market_kind: str) -> str:
    return "competitive_dynamics" if market_kind == "cd" else "market_landscape"


def _market_name(market_row: Mapping[str, Any], market_catalog_row: Mapping[str, Any]) -> str | None:
    for key in ("name", "market_name", "ml_name", "cd_name"):
        value = market_catalog_row.get(key) or market_row.get(key)
        if value:
            return str(value)
    return None
