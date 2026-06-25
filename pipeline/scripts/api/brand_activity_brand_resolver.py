from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping, Sequence

from pipeline.etl.io.mart.molecule_normalize import split_molecule_components
from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en
from pipeline.scripts.api import db
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
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.types import quote_identifier


MAX_BRAND_SET_SIZE: Final = 6


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


def resolve_brand_set(
    *,
    view_name: str,
    market_id: str | None,
    selected_brand: str,
    filter_payload: Mapping[str, Any] | None = None,
    ranking_quarters: Sequence[str] | None = None,
) -> BrandSetResolution | None:
    """Resolve the Brand Activity selected brand plus top sales competitors."""

    view = view_config(view_name)
    resolved_market_id = market_id
    if not resolved_market_id and view_name == "strategic_ml":
        resolved_market_id = _ml_id_for_brand(selected_brand)
    if not resolved_market_id:
        raise BrandSetInputError("market_id is required")

    brand_rows = tuple(_fetch_brand_rows(view, resolved_market_id))
    if not brand_rows:
        return None
    market_row = _fetch_market_row(view, resolved_market_id)
    if market_row is None:
        return None
    ranking = _ranking_for_quarter(market_row, view.ranking_column, ranking_quarters)
    brand_meta = _brand_meta_by_key(brand_rows, has_is_jw=view.has_is_jw)
    if selected_brand not in brand_meta:
        return None
    applied_filter = applied_brand_filter(view_name, resolved_market_id, filter_payload or {})
    candidates = _brand_candidates(view_name, brand_rows, brand_meta, ranking)
    choices = _select_choices(candidates, selected_brand=selected_brand, applied_filter=applied_filter)
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
    )


def view_config(view_name: str) -> ViewConfig:
    """Return the mart table registry for one Brand Activity view."""

    if view_name == "general":
        return ViewConfig("mart_general_brand_metric", "mart_general_market_metric", "atc4_code", "atc4_desc", "brand_ranking", False)
    if view_name == "strategic_ml":
        return ViewConfig("mart_strategic_ml_brand_metric", "mart_strategic_ml_market_metric", "ml_id", "ml_name", "brand_ranking_stacked", True)
    raise BrandSetInputError(f"unsupported view: {view_name}")


def _ml_id_for_brand(brand: str) -> str:
    """Resolve one strategic ML market for a selected brand in the ranking scope."""

    rows = db.fetch_all(
        f"""
        SELECT DISTINCT ml_id
        FROM {quote_identifier(config.db_name)}.`mart_strategic_ml_brand_metric`
        WHERE source = %s AND measure = %s AND (brand_key = %s OR brand_name = %s)
        ORDER BY ml_id
        """,
        (SOURCE, RANKING_MEASURE, brand, brand),
    )
    market_ids = tuple(str(row["ml_id"]) for row in rows if row.get("ml_id"))
    if not market_ids:
        raise BrandSetInputError("brand not in any ml market")
    if len(market_ids) > 1:
        raise BrandSetInputError(f"ambiguous ml market for brand; pass market_id: {', '.join(market_ids)}")
    return market_ids[0]


def _fetch_brand_rows(view: ViewConfig, market_id: str) -> list[JsonMap]:
    is_jw = "is_jw" if view.has_is_jw else "0 AS is_jw"
    overlay = "overlay_data" if view.has_is_jw else "NULL AS overlay_data"
    return db.fetch_all(
        f"""
        SELECT DISTINCT brand_key, brand_name, {is_jw}, by_dimension, {overlay}, metric_history
        FROM {quote_identifier(config.db_name)}.{quote_identifier(view.brand_table)}
        WHERE {view.market_key} = %s AND source = %s AND measure = %s
        ORDER BY brand_key
        """,
        (market_id, SOURCE, RANKING_MEASURE),
    )


def _fetch_market_row(view: ViewConfig, market_id: str) -> JsonMap | None:
    return db.fetch_one(
        f"""
        SELECT {view.market_key}, {view.market_name_column}, market_size_series, {view.ranking_column}
        FROM {quote_identifier(config.db_name)}.{quote_identifier(view.market_table)}
        WHERE {view.market_key} = %s AND source = %s AND measure = %s
        LIMIT 1
        """,
        (market_id, SOURCE, RANKING_MEASURE),
    )


def _brand_candidates(
    view_name: str,
    rows: tuple[JsonMap, ...],
    metas: dict[str, BrandMeta],
    ranking: JsonMap,
) -> tuple[BrandCandidate, ...]:
    rank_by_key = {text(item.get("brand_key")): item for item in _ranking_items(ranking)}
    general_molecules = general_molecules_by_product(metas) if view_name == "general" else {}
    candidates: list[BrandCandidate] = []
    for row in rows:
        brand_key = str(row["brand_key"])
        meta = metas[brand_key]
        rank_item = rank_by_key.get(brand_key, {})
        metric = json_map(json_map(row.get("metric_history")).get(ranking["quarter"]))
        candidates.append(
            BrandCandidate(
                meta=meta,
                dimensions=_dimensions(view_name, row, meta, general_molecules),
                sales_rank=int_or_none(rank_item.get("rank")) or int_or_none(metric.get("rank")),
                sales_value=float_value(rank_item.get("raw_value")) or float_value(metric.get("raw_value")),
            )
        )
    return tuple(candidates)


def _select_choices(
    candidates: tuple[BrandCandidate, ...],
    *,
    selected_brand: str,
    applied_filter: JsonMap,
) -> tuple[BrandChoice, ...]:
    selected = next((candidate for candidate in candidates if candidate.meta.brand_key == selected_brand), None)
    if selected is None:
        return ()
    competitors = [
        candidate
        for candidate in candidates
        if candidate.meta.brand_key != selected_brand and _passes_filter(candidate, applied_filter)
    ]
    competitors.sort(key=lambda candidate: (-candidate.sales_value, candidate.sales_rank or 999_999, candidate.meta.brand_key))
    ordered = [selected, *competitors[: MAX_BRAND_SET_SIZE - 1]]
    return tuple(
        BrandChoice(
            brand_key=candidate.meta.brand_key,
            brand_name=candidate.meta.brand_name,
            sales_rank=candidate.sales_rank,
            is_selected=candidate.meta.brand_key == selected_brand,
        )
        for candidate in ordered
    )


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
) -> dict[str, tuple[str, ...]]:
    by_dimension = json_map(row.get("by_dimension"))
    overlay = json_map(row.get("overlay_data"))
    atc4_values = _text_values(by_dimension.get("atc4_code"), overlay.get("allowed_atc4_codes"))
    dimensions = {"atc4": tuple(value.upper() for value in atc4_values)}
    if view_name == "general":
        molecule_values = [value for code in meta.product_codes for value in general_molecules.get(code, ())]
        dimensions["molecule"] = _unique(molecule_values)
        return dimensions
    dimensions["molecule"] = _unique(
        component.norm
        for value in _text_values(by_dimension.get("molecule"), overlay.get("molecule"))
        for component in split_molecule_components(value)
    )
    dimensions["class"] = _unique(_text_values(by_dimension.get("class"), by_dimension.get("class_1"), by_dimension.get("class_2"), overlay.get("class"), overlay.get("class_1"), overlay.get("class_2")))
    return dimensions


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
