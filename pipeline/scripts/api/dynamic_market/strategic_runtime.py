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
from threading import Lock
from typing import Any

from pipeline.etl.io.mart.brand_key_normalize import normalize_brand_name
from pipeline.scripts.api import db
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.dynamic_market.resolvers import expand_atc4_for_source, normalize_source
from pipeline.scripts.api.dynamic_market.types import DynamicMarketInputError, quote_identifier
from pipeline.scripts.api.market_definition_display import apply_cd_market_definition
from pipeline.scripts.api.models.dynamic_market import DynamicMarketAnalysisLevelFilters


ETL_DIR = Path(__file__).resolve().parents[2] / "etl"
if str(ETL_DIR) not in sys.path:
    sys.path.insert(0, str(ETL_DIR))

from pipeline.scripts.etl import build_cache_cause as cause_builder  # noqa: E402
from pipeline.scripts.etl.ubist_channel_resolver import strategic_channel_totals_context  # noqa: E402


JsonRow = dict[str, Any]
_STRATEGIC_BUILD_LOCK = Lock()


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
    """Build one strategic response while bounding shared builder cache memory."""

    with _STRATEGIC_BUILD_LOCK:
        _clear_cause_builder_runtime_caches()
        try:
            return _build_strategic_payload(
                mart_db=mart_db,
                ml_id=ml_id,
                cd_market_id=cd_market_id,
                focus_brand_key=focus_brand_key,
                source=source,
                measure=measure,
                analysis_level=analysis_level,
            )
        finally:
            _clear_cause_builder_runtime_caches()


def _build_strategic_payload(
    *,
    mart_db: str,
    ml_id: str | None,
    cd_market_id: str | None,
    focus_brand_key: str | None,
    source: str,
    measure: str,
    analysis_level: DynamicMarketAnalysisLevelFilters,
) -> JsonRow:
    """Assemble a strategic response from mart rows."""

    market_kind, view_source_id = _resolve_market_id(ml_id=ml_id, cd_market_id=cd_market_id)
    mart_source = normalize_source(source)
    source_api = cause_builder.api_source(mart_source)
    response_market_id = _response_market_id(market_kind, view_source_id)
    has_runtime_filter = bool(_selected_filters(source=mart_source, analysis_level=analysis_level))

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
    if has_runtime_filter:
        market_row = _market_row_for_filtered_rows(market_row, filtered_rows)

    market_catalog_row = _catalog_row(market_kind, view_source_id)
    if mart_source == "ubist":
        filtered_rows = _with_channel_specialty_matrices(
            mart_db=mart_db,
            rows=filtered_rows,
            source=mart_source,
            measure=measure,
            market_catalog_row=market_catalog_row,
        )
    strategic_brand = _strategic_brand_catalog()
    with strategic_channel_totals_context(filtered_rows):
        raw_payload = cause_builder.build_response(
            brand_row=brand_row,
            market_row=market_row,
            sibling_rows=filtered_rows,
            view_type=_view_type(market_kind),
            market_id=response_market_id,
            source=source_api,
            measure=measure,
            view_source_id=view_source_id,
            market_name=_market_name(market_row, market_catalog_row),
            market_sources=_market_sources(market_catalog_row, source_api),
            market_catalog_row=market_catalog_row,
            strategic_brand=strategic_brand,
        )
    composed = compose_cached_json(raw_payload, measure=measure)
    if not isinstance(composed, dict):
        raise DynamicMarketInputError("strategic payload composition did not return an object")
    composed["markets"] = [{"market_id": response_market_id, "is_primary": True}]
    apply_cd_market_definition(composed, view_source_id)
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
    for key, values in source_filters.model_dump(by_alias=True).items():
        if key not in _STRATEGIC_NARROWING_KEYS:
            continue
        clean = tuple(str(value).strip() for value in values if str(value).strip())
        if clean:
            if key == "atc4":
                clean = expand_atc4_for_source(clean, source=source)
            selected[key] = clean
    return selected


_STRATEGIC_NARROWING_KEYS = frozenset({"atc3", "atc4"})


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
        "class": ("class", "class_name", "market_class"),
        "mfr_name_kor": ("mfr_name_kor", "mfr", "manufacturer", "company_name"),
        "mfr": ("mfr", "mfr_name_kor", "manufacturer", "company_name"),
        "molecule": ("molecule", "molecule_desc"),
        "molecule_strength": ("molecule_strength", "strength_pack", "성분용량"),
        "strength_pack": ("strength_pack", "molecule_strength", "성분용량"),
        "ox_gx": ("ox_gx", "oxgx"),
        "form": ("form", "dosage_form", "제형"),
        "route": ("route", "투여경로"),
        "reimbursement": ("reimbursement", "nhi_type", "nhi", "급여구분"),
        "nhi": ("nhi", "nhi_type", "급여구분"),
        "nhi_type": ("nhi_type", "nhi", "급여구분"),
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


def _with_channel_specialty_matrices(
    *,
    mart_db: str,
    rows: Sequence[JsonRow],
    source: str,
    measure: str,
    market_catalog_row: Mapping[str, Any],
) -> list[JsonRow]:
    """Attach raw UBIST facility-specialty matrices for real-time channel fill.

    Strategic brand rows keep market metrics, while the raw channel grain lives
    in the general mart at brand×ATC4. Joining by brand alone can leak adjacent
    ATC4 rows for brands that appear in multiple markets, so this loader scopes
    the matrix by the selected row dimensions and the MI Master ATC4 catalog.
    """

    copied = [dict(row) for row in rows]
    if not copied:
        return copied
    brand_keys = tuple(sorted({str(row.get("brand_key") or "").strip() for row in copied if row.get("brand_key")}))
    if not brand_keys:
        return copied
    atc4_codes = _matrix_scope_atc4(rows=copied, market_catalog_row=market_catalog_row)
    matrices = _fetch_channel_specialty_matrices(
        mart_db=mart_db,
        brand_keys=brand_keys,
        atc4_codes=atc4_codes,
        source=source,
        measure=measure,
    )
    if not matrices:
        return copied
    for row in copied:
        brand_key = str(row.get("brand_key") or "").strip()
        matrix = matrices.get(brand_key)
        if matrix:
            row["channel_specialty_matrix"] = matrix
    return copied


def _matrix_scope_atc4(*, rows: Sequence[Mapping[str, Any]], market_catalog_row: Mapping[str, Any]) -> tuple[str, ...]:
    row_codes: set[str] = set()
    for row in rows:
        row_codes.update(_dimension_text_values(_decode_object(row.get("by_dimension")), "atc4_code", "atc4"))
    catalog_codes = _catalog_atc4_codes(market_catalog_row)
    if row_codes and catalog_codes:
        scoped = row_codes & set(catalog_codes)
        if scoped:
            return tuple(sorted(scoped))
    if row_codes:
        return tuple(sorted(row_codes))
    return catalog_codes


def _catalog_atc4_codes(row: Mapping[str, Any]) -> tuple[str, ...]:
    for key in ("atc_codes_json", "atc4_codes", "atc4_code"):
        value = row.get(key)
        if isinstance(value, list):
            values = value
        elif isinstance(value, str) and value.strip():
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                values = [item.strip() for item in value.split(",")]
            else:
                values = decoded if isinstance(decoded, list) else [decoded]
        else:
            values = []
        clean = tuple(sorted({str(item).strip().upper() for item in values if str(item).strip()}))
        if clean:
            return clean
    return ()


def _dimension_text_values(dimensions: Mapping[str, Any], *keys: str) -> set[str]:
    values: set[str] = set()
    for key in keys:
        raw = dimensions.get(key)
        if isinstance(raw, list):
            values.update(str(item).strip().upper() for item in raw if str(item).strip())
        elif raw not in (None, ""):
            values.add(str(raw).strip().upper())
    return values


def _fetch_channel_specialty_matrices(
    *,
    mart_db: str,
    brand_keys: tuple[str, ...],
    atc4_codes: tuple[str, ...],
    source: str,
    measure: str,
) -> dict[str, dict[str, Any]]:
    if not brand_keys:
        return {}
    brand_placeholders = ", ".join(["%s"] * len(brand_keys))
    params: list[Any] = [source, measure, *brand_keys]
    atc4_where = ""
    if atc4_codes:
        atc4_where = "AND atc4_code IN (" + ", ".join(["%s"] * len(atc4_codes)) + ")"
        params.extend(atc4_codes)
    rows = db.fetch_all(
        f"""
        SELECT brand_key, channel_specialty_matrix
        FROM {quote_identifier(mart_db)}.mart_general_brand_metric
        WHERE source = %s
          AND measure = %s
          AND brand_key IN ({brand_placeholders})
          {atc4_where}
        """,
        params,
    )
    matrices: dict[str, dict[str, Any]] = {}
    for row in rows:
        brand_key = str(row.get("brand_key") or "").strip()
        matrix = _decode_object(row.get("channel_specialty_matrix"))
        if brand_key and matrix:
            _merge_channel_specialty_matrix(matrices.setdefault(brand_key, {}), matrix)
    return matrices


def _merge_channel_specialty_matrix(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for facility, specialties in source.items():
        if not isinstance(specialties, Mapping):
            continue
        target_facility = target.setdefault(str(facility), {})
        if not isinstance(target_facility, dict):
            continue
        for specialty, series in specialties.items():
            if not isinstance(series, Mapping):
                continue
            target_series = target_facility.setdefault(str(specialty), {})
            if not isinstance(target_series, dict):
                continue
            for period, value in series.items():
                try:
                    numeric = float(value or 0.0)
                except (TypeError, ValueError):
                    numeric = 0.0
                period_text = str(period)
                target_series[period_text] = float(target_series.get(period_text) or 0.0) + numeric


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


@lru_cache(maxsize=4)
def _ml_market_catalog() -> Mapping[str, JsonRow]:
    rows = db.fetch_all(
        """
        SELECT *
        FROM catalog_ml_market
        ORDER BY ml_id
        """
    )
    return {str(row["ml_id"]): dict(row) for row in rows if row.get("ml_id") is not None}


@lru_cache(maxsize=4)
def _cd_market_catalog() -> Mapping[str, JsonRow]:
    rows = db.fetch_all(
        """
        SELECT *
        FROM catalog_cd_market
        ORDER BY cd_id
        """
    )
    catalog: dict[str, JsonRow] = {}
    for row in rows:
        cd_id = row.get("cd_id")
        if cd_id is None:
            continue
        record = dict(row)
        record["cd_market_id"] = cd_id
        catalog[str(cd_id)] = record
    return catalog


@lru_cache(maxsize=2)
def _strategic_brand_catalog() -> list[JsonRow]:
    return db.fetch_all(
        """
        SELECT *
        FROM catalog_strategic_brand
        ORDER BY brand_id
        """
    )


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
