from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping, Sequence

from pipeline.etl.io.mart.filter_dimension_metric import FILTER_DIMENSION_TABLE
from pipeline.etl.io.mart.molecule_normalize import split_molecule_components
from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en
from pipeline.scripts.api import db
from pipeline.scripts.api.brand_activity_channel_axis import (
    audit_code_keys,
    audit_code_sales_value,
    parse_audit_code_axis,
)
from pipeline.scripts.api.brand_activity_brand_filters import applied_brand_filter
from pipeline.scripts.api.brand_activity_brand_molecules import general_molecules_by_product
from pipeline.scripts.api.brand_activity_csd_shared import (
    RANKING_MEASURE,
    SOURCE,
    BrandChoice,
    BrandMeta,
    JsonMap,
    ViewConfig,
    float_value,
    int_or_none,
    json_map,
    text,
)
from pipeline.scripts.api.catalog import get_display_brand
from pipeline.scripts.api.competitor_ranking import CompetitorRankItem, select_top_competitors
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.channel_axis import ChannelAxisFilter
from pipeline.scripts.api.dynamic_market.types import quote_identifier
from pipeline.scripts.api.market_scope.catalog import MarketScopeCatalog
from pipeline.scripts.api.market_scope.types import MarketScopeOption, OptionType, ViewFamily


MAX_BRAND_SET_SIZE: Final = 6
GENERAL_IQVIA_SIDE_CAR_DIMENSIONS: Final = ("mfr", "molecule_type", "molecule_desc", "pack", "strength", "nhi")


class BrandSetInputError(RuntimeError):
    """Raised when a Brand Activity brand-set request cannot be parsed."""


class BrandSetResolutionError(RuntimeError):
    """Raised when a required brand-set bridge cannot be resolved."""


@dataclass(frozen=True, slots=True)
class BrandCandidate:
    """One mart brand with filter dimensions and sales-ranking evidence."""

    meta: BrandMeta
    dimensions: dict[str, tuple[str, ...]]
    sales_rank: int | None
    sales_value: float


@dataclass(frozen=True, slots=True)
class BrandSetResolution:
    """Resolved selected brand plus sales-ranked competitors."""

    view_name: str
    market_id: str
    selected_brand: str
    view: ViewConfig
    market_row: JsonMap
    brand_rows: tuple[JsonMap, ...]
    brand_meta: dict[str, BrandMeta]
    choices: tuple[BrandChoice, ...]
    candidates: tuple[BrandCandidate, ...]
    ranking_quarter: str
    applied_filter: JsonMap
    channel_axis: ChannelAxisFilter | None = None


def resolve_brand_set(
    *,
    view_name: str,
    market_id: str | None,
    selected_brand: str,
    filter_payload: Mapping[str, Any] | None = None,
    ranking_quarters: Sequence[str] | None = None,
    source: str = SOURCE,
    rank_by_latest_period: bool = False,
) -> BrandSetResolution | None:
    """Resolve the Brand Activity selected brand plus top sales competitors."""

    view = view_config(view_name)
    raw_filter_payload = filter_payload or {}
    market_scope_market_id = _market_scope_market_id(
        view_name=view_name,
        selected_brand=selected_brand,
        filter_payload=raw_filter_payload,
    )
    resolved_market_id = market_scope_market_id or market_id
    if not resolved_market_id and view_name == "strategic_ml":
        resolved_market_id = _ml_id_for_brand(selected_brand, source=source)
    if not resolved_market_id:
        raise BrandSetInputError("market_id is required")

    brand_rows = tuple(_fetch_brand_rows(view, resolved_market_id, source=source))
    if not brand_rows:
        return None
    market_row = _fetch_market_row(view, resolved_market_id, source=source)
    if market_row is None:
        return None
    ranking = _ranking_for_quarter(market_row, view.ranking_column, ranking_quarters)
    brand_meta = _brand_meta_by_key(brand_rows, has_is_jw=view.has_is_jw)
    if selected_brand not in brand_meta:
        return None
    effective_filter_payload = _filter_payload_for_effective_market(raw_filter_payload, resolved_market_id, market_scope_market_id is not None)
    applied_filter = applied_brand_filter(view_name, resolved_market_id, effective_filter_payload)
    channel_axis = parse_audit_code_axis(effective_filter_payload) if view_name == "general" else None
    validate_audit_code_axis(brand_rows, channel_axis)
    candidates = _brand_candidates(
        view_name,
        brand_rows,
        brand_meta,
        ranking,
        ranking_quarters=ranking_quarters,
        audit_code_axis=channel_axis,
        source=source,
    )
    choices = _select_choices(
        candidates,
        selected_brand=selected_brand,
        applied_filter=applied_filter,
        rank_by_latest_period=rank_by_latest_period,
    )
    return BrandSetResolution(
        view_name=view_name,
        market_id=resolved_market_id,
        selected_brand=selected_brand,
        view=view,
        market_row=market_row,
        brand_rows=brand_rows,
        brand_meta=brand_meta,
        choices=choices,
        candidates=candidates,
        ranking_quarter=ranking["quarter"],
        applied_filter=applied_filter,
        channel_axis=channel_axis,
    )


def _market_scope_market_id(
    *,
    view_name: str,
    selected_brand: str,
    filter_payload: Mapping[str, Any],
) -> str | None:
    market_scope = _market_scope_payload(filter_payload)
    if market_scope is None:
        return None
    if view_name != "general":
        raise BrandSetInputError("unsupported_view_for_market_scope")
    option_id = text(market_scope.get("option_id"))
    if not option_id:
        raise BrandSetInputError("invalid_market_scope")
    catalog = MarketScopeCatalog.load_default()
    option = _market_scope_option(catalog, option_id, selected_brand)
    if option is None:
        raise BrandSetInputError("invalid_market_scope")
    if option.option_type is not OptionType.GROUP_UNION:
        raise BrandSetInputError("invalid_market_scope")
    member_name = text(market_scope.get("member"))
    if not member_name or member_name == "전체":
        raise BrandSetInputError("unsupported_market_scope_member")
    member = next((candidate for candidate in option.members if candidate.brand_name == member_name), None)
    if member is None:
        raise BrandSetInputError("invalid_market_scope_member")
    if member.member_status != "present":
        raise BrandSetInputError("unsupported_market_scope_member")
    if len(member.atc4_set) != 1:
        raise BrandSetInputError("unsupported_member_scope")
    return member.atc4_set[0].upper()


def _market_scope_payload(filter_payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = filter_payload.get("market_scope")
    return value if isinstance(value, Mapping) else None


def _market_scope_option(catalog: MarketScopeCatalog, option_id: str, selected_brand: str) -> MarketScopeOption | None:
    options = {option.option_id: option for option in catalog.group_options}
    for option in catalog.options_for_brand(selected_brand, view_family=ViewFamily.STRATEGY):
        options.setdefault(option.option_id, option)
    return options.get(option_id)


def _filter_payload_for_effective_market(filter_payload: Mapping[str, Any], market_id: str, market_scope_used: bool) -> Mapping[str, Any]:
    if not market_scope_used:
        return filter_payload
    payload = dict(filter_payload)
    payload["atc4"] = [market_id]
    return payload


def view_config(view_name: str) -> ViewConfig:
    """Return the mart table registry for one Brand Activity view."""

    if view_name == "general":
        return ViewConfig("mart_general_brand_metric", "mart_general_market_metric", "atc4_code", "atc4_desc", "brand_ranking", False)
    if view_name == "strategic_ml":
        return ViewConfig("mart_strategic_ml_brand_metric", "mart_strategic_ml_market_metric", "ml_id", "ml_name", "brand_ranking_stacked", True)
    if view_name == "strategic_cd":
        return ViewConfig(
            "mart_strategic_cd_brand_metric",
            "mart_strategic_cd_market_metric",
            "cd_market_id",
            "cd_market_name",
            "brand_ranking_stacked",
            True,
        )
    raise BrandSetInputError(f"unsupported view: {view_name}")


def _ml_id_for_brand(brand: str, *, source: str = SOURCE) -> str:
    """Resolve one strategic ML market for a selected brand in the ranking scope."""

    rows = db.fetch_all(
        f"""
        SELECT DISTINCT ml_id
        FROM {quote_identifier(config.db_name)}.`mart_strategic_ml_brand_metric`
        WHERE source = %s AND measure = %s AND (brand_key = %s OR brand_name = %s)
        ORDER BY ml_id
        """,
        (source, RANKING_MEASURE, brand, brand),
    )
    market_ids = tuple(str(row["ml_id"]) for row in rows if row.get("ml_id"))
    if not market_ids:
        raise BrandSetInputError("brand not in any ml market")
    if len(market_ids) > 1:
        # Expected 1:1 in the IQVIA sales scope. If violated, fail loudly
        # rather than silently returning a possibly-wrong market.
        raise BrandSetInputError(
            f"ambiguous ml market for brand: {', '.join(market_ids)}"
        )
    return market_ids[0]


def _fetch_brand_rows(view: ViewConfig, market_id: str, *, source: str = SOURCE) -> list[JsonMap]:
    is_jw = "is_jw" if view.has_is_jw else "0 AS is_jw"
    overlay = "overlay_data" if view.has_is_jw else "NULL AS overlay_data"
    audit_code_matrix = "audit_code_matrix" if view.brand_table == "mart_general_brand_metric" else "NULL AS audit_code_matrix"
    return db.fetch_all(
        f"""
        SELECT DISTINCT brand_key, brand_name, {is_jw}, by_dimension, {overlay}, metric_history, {audit_code_matrix}
        FROM {quote_identifier(config.db_name)}.{quote_identifier(view.brand_table)}
        WHERE {view.market_key} = %s AND source = %s AND measure = %s
        ORDER BY brand_key
        """,
        (market_id, source, RANKING_MEASURE),
    )


def _fetch_market_row(view: ViewConfig, market_id: str, *, source: str = SOURCE) -> JsonMap | None:
    return db.fetch_one(
        f"""
        SELECT {view.market_key}, {view.market_name_column}, market_size_series, {view.ranking_column}
        FROM {quote_identifier(config.db_name)}.{quote_identifier(view.market_table)}
        WHERE {view.market_key} = %s AND source = %s AND measure = %s
        LIMIT 1
        """,
        (market_id, source, RANKING_MEASURE),
    )


def validate_audit_code_axis(rows: tuple[JsonMap, ...], channel_axis: ChannelAxisFilter | None) -> None:
    """Reject requested IQVIA audit codes that are absent from the dynamic matrix keys."""

    if channel_axis is None or not channel_axis.is_active:
        return
    supported = {code for row in rows for code in audit_code_keys(row)}
    unsupported = tuple(code for code in channel_axis.audit_codes if code not in supported)
    if unsupported:
        raise BrandSetInputError(f"unsupported audit_code: {', '.join(unsupported)}")


def _brand_candidates(
    view_name: str,
    rows: tuple[JsonMap, ...],
    metas: dict[str, BrandMeta],
    ranking: JsonMap,
    *,
    ranking_quarters: Sequence[str] | None = None,
    audit_code_axis: ChannelAxisFilter | None = None,
    source: str = SOURCE,
) -> tuple[BrandCandidate, ...]:
    rank_by_key = {text(item.get("brand_key")): item for item in _ranking_items(ranking)}
    general_molecules = general_molecules_by_product(metas) if view_name == "general" else {}
    general_sidecar = _general_sidecar_dimensions(rows) if view_name == "general" and source == SOURCE else {}
    candidates: list[BrandCandidate] = []
    for row in rows:
        brand_key = str(row["brand_key"])
        meta = metas[brand_key]
        rank_item = rank_by_key.get(brand_key, {})
        metric = json_map(json_map(row.get("metric_history")).get(ranking["quarter"]))
        candidates.append(
            BrandCandidate(
                meta=meta,
                dimensions=_dimensions(view_name, row, meta, general_molecules, general_sidecar.get(brand_key, {})),
                sales_rank=int_or_none(rank_item.get("rank")) or int_or_none(metric.get("rank")),
                sales_value=_candidate_sales_value(row, ranking=ranking, ranking_quarters=ranking_quarters, audit_code_axis=audit_code_axis),
            )
        )
    return tuple(candidates)


def _select_choices(
    candidates: tuple[BrandCandidate, ...],
    *,
    selected_brand: str,
    applied_filter: JsonMap,
    rank_by_latest_period: bool = False,
) -> tuple[BrandChoice, ...]:
    selected = next((candidate for candidate in candidates if candidate.meta.brand_key == selected_brand), None)
    if selected is None:
        return ()
    eligible = [
        selected,
        *[
            candidate
            for candidate in candidates
            if candidate.meta.brand_key != selected_brand and _passes_filter(candidate, applied_filter)
        ],
    ]
    if rank_by_latest_period:
        competitors = sorted(
            (
                candidate
                for candidate in eligible
                if candidate.meta.brand_key != selected_brand and candidate.sales_rank is not None
            ),
            key=lambda candidate: (candidate.sales_rank, candidate.meta.brand_key),
        )[: MAX_BRAND_SET_SIZE - 1]
        ordered = (selected, *competitors)
    else:
        ordered = select_top_competitors(
            tuple(CompetitorRankItem(candidate.meta.brand_key, candidate.sales_value, candidate) for candidate in eligible),
            selected_brand_key=selected_brand,
            top_n=MAX_BRAND_SET_SIZE - 1,
        )
    return tuple(
        BrandChoice(
            brand_key=candidate.meta.brand_key,
            brand_name=candidate.meta.brand_name,
            sales_rank=candidate.sales_rank,
            is_selected=candidate.meta.brand_key == selected_brand,
        )
        for candidate in ordered
    )


def _candidate_sales_value(
    row: JsonMap,
    *,
    ranking: JsonMap,
    ranking_quarters: Sequence[str] | None,
    audit_code_axis: ChannelAxisFilter | None,
) -> float:
    metric_history = json_map(row.get("metric_history"))
    periods = tuple(ranking_quarters or sorted(metric_history) or (str(ranking["quarter"]),))
    if audit_code_axis:
        return sum(audit_code_sales_value(row, audit_code_axis, period) for period in periods)
    return sum(float_value(json_map(metric_history.get(period)).get("raw_value")) or 0.0 for period in periods)


def _passes_filter(candidate: BrandCandidate, applied_filter: JsonMap) -> bool:
    for dimension, raw_values in applied_filter.items():
        values = tuple(value for value in raw_values if isinstance(value, str)) if isinstance(raw_values, list) else ()
        if values and not (set(values) & set(candidate.dimensions.get(dimension, ()))):
            return False
    return True


def _dimensions(
    view_name: str,
    row: JsonMap,
    meta: BrandMeta,
    general_molecules: dict[str, tuple[str, ...]],
    sidecar_dimensions: Mapping[str, tuple[str, ...]] | None = None,
) -> dict[str, tuple[str, ...]]:
    by_dimension = json_map(row.get("by_dimension"))
    overlay = json_map(row.get("overlay_data"))
    atc4_values = _text_values(by_dimension.get("atc4_code"), overlay.get("allowed_atc4_codes"))
    dimensions = {"atc4": tuple(value.upper() for value in atc4_values)}
    if view_name == "general":
        molecule_values = [value for code in meta.product_codes for value in general_molecules.get(code, ())]
        dimensions["molecule"] = _unique(molecule_values)
        for dimension, values in (sidecar_dimensions or {}).items():
            dimensions[dimension] = _unique(values)
        return dimensions
    dimensions["molecule"] = _unique(
        component.norm
        for value in _text_values(by_dimension.get("molecule"), overlay.get("molecule"))
        for component in split_molecule_components(value)
    )
    dimensions["class"] = _unique(_text_values(by_dimension.get("class"), by_dimension.get("class_1"), by_dimension.get("class_2"), overlay.get("class"), overlay.get("class_1"), overlay.get("class_2")))
    return dimensions


def _general_sidecar_dimensions(rows: tuple[JsonMap, ...]) -> dict[str, dict[str, tuple[str, ...]]]:
    """Return IQVIA product-level sidecar dimensions grouped by brand key."""

    brand_keys = tuple(sorted({str(row.get("brand_key")) for row in rows if row.get("brand_key")}))
    atc4_codes = tuple(
        sorted(
            {
                value.upper()
                for row in rows
                for value in _text_values(json_map(row.get("by_dimension")).get("atc4_code"))
                if value
            }
        )
    )
    if not brand_keys or not atc4_codes:
        return {}
    rows = db.fetch_all(
        f"""
        SELECT DISTINCT brand_key, dimension_type, dimension_value_norm
        FROM {quote_identifier(config.db_name)}.{quote_identifier(FILTER_DIMENSION_TABLE)}
        WHERE source = %s
          AND measure = %s
          AND brand_key IN ({_placeholders(brand_keys)})
          AND atc4_code IN ({_placeholders(atc4_codes)})
          AND dimension_type IN ({_placeholders(GENERAL_IQVIA_SIDE_CAR_DIMENSIONS)})
        ORDER BY brand_key, dimension_type, dimension_value_norm
        """,
        (SOURCE, RANKING_MEASURE, *brand_keys, *atc4_codes, *GENERAL_IQVIA_SIDE_CAR_DIMENSIONS),
    )
    collected: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        brand_key = text(row.get("brand_key"))
        dimension = text(row.get("dimension_type"))
        value = text(row.get("dimension_value_norm"))
        if not brand_key or dimension not in GENERAL_IQVIA_SIDE_CAR_DIMENSIONS or not value:
            continue
        values = collected.setdefault(brand_key, {}).setdefault(dimension, [])
        if value not in values:
            values.append(value)
    return {
        brand_key: {dimension: tuple(values) for dimension, values in dimensions.items()}
        for brand_key, dimensions in collected.items()
    }


def _placeholders(values: Sequence[str]) -> str:
    return ", ".join(["%s"] * len(values))


def _brand_meta_by_key(rows: tuple[JsonMap, ...], *, has_is_jw: bool) -> dict[str, BrandMeta]:
    metas: dict[str, BrandMeta] = {}
    for row in rows:
        brand_key = str(row["brand_key"])
        products = tuple(sorted({normalize_iqvia_en(code) for code in _product_codes(row.get("by_dimension"))}))
        is_jw = bool(row.get("is_jw")) if has_is_jw else get_display_brand(brand_key) is not None
        metas[brand_key] = BrandMeta(brand_key, str(row.get("brand_name") or brand_key), products, is_jw)
    return metas


def _product_codes(value: Any) -> list[str]:
    products = json_map(value).get("products")
    if not isinstance(products, list):
        return []
    return [str(item.get("product_code")) for item in products if isinstance(item, dict) and item.get("product_code")]


def _ranking_for_quarter(row: JsonMap, ranking_column: str, quarters: Sequence[str] | None) -> JsonMap:
    ranking = json_map(row.get(ranking_column))
    if quarters:
        quarter = next((quarter for quarter in reversed(tuple(quarters)) if quarter in ranking), "")
    else:
        quarter = ""
    if not quarter:
        quarter = sorted(ranking)[-1] if ranking else ""
    items = ranking.get(quarter, [])
    return {"quarter": quarter, "items": items if isinstance(items, list) else []}


def _ranking_items(ranking: JsonMap) -> list[JsonMap]:
    items = ranking.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _text_values(*values: Any) -> tuple[str, ...]:
    collected: list[str] = []
    for value in values:
        if isinstance(value, list | tuple):
            collected.extend(str(item).strip() for item in value if str(item).strip())
        elif text(value).strip():
            collected.append(text(value).strip())
    return _unique(collected)


def _unique(values: Any) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value).strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)
