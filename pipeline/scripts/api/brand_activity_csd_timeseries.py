from __future__ import annotations

from typing import Any, Mapping

from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en
from pipeline.scripts.api import db
from pipeline.scripts.api.brand_activity_csd_shared import (
    PUBLIC_MEASURES,
    RANKING_MEASURE,
    RX_MEASURES,
    SOURCE,
    BrandChoice,
    BrandMeta,
    CsdCrosswalk,
    CsdTimeseriesAmbiguousMarketError,
    CsdTimeseriesInputError,
    JsonMap,
    ViewConfig,
    display_csd_market,
    first,
    float_value,
    full_quarters_from_months,
    int_or_none,
    json_map,
    normalized_product_overlap,
    period_ym_to_quarter,
    ratio,
    select_ranked_brands,
    text,
)
from pipeline.scripts.api.catalog import get_display_brand
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.types import quote_identifier


def get_csd_timeseries(payload: Mapping[str, Any]) -> JsonMap | None:
    """Return integrated CSD activity and IQVIA prescription series."""

    request = _parse_request(payload)
    view = _view_config(request["view"])
    brand_rows = _fetch_brand_rows(view, request["market_id"])
    if not brand_rows:
        return None
    brand_meta = _brand_meta_by_key(brand_rows, has_is_jw=view.has_is_jw)
    market_row = _fetch_market_row(view, request["market_id"], RANKING_MEASURE)
    if market_row is None:
        return None
    all_csd_months = [str(row["period_ym"]) for row in db.fetch_all(_sql_csd_months())]
    quarters = _requested_quarters(full_quarters_from_months(all_csd_months), request["window"])
    if not quarters:
        return None
    ranking = _ranking_for_quarter(market_row, view.ranking_column, quarters)
    ranking_items = _with_selected_rank(ranking["items"], brand_rows, request["selected_brand"], ranking["quarter"])
    choices = select_ranked_brands(ranking_items, selected_brand=request["selected_brand"])
    selected_meta = brand_meta.get(request["selected_brand"])
    if selected_meta is None:
        return None
    mart_codes = {code for meta in brand_meta.values() for code in meta.product_codes}
    crosswalk = resolve_csd_market(mart_codes)
    rx_rows = _fetch_rx_rows(view, request["market_id"], tuple(choice.brand_key for choice in choices))
    activity = _activity_series(crosswalk.market, choices, brand_meta, quarters)
    return {
        "scope": _scope_payload(request, view, market_row, selected_meta, ranking, crosswalk, quarters),
        "brands": [_brand_payload(choice, brand_meta, rx_rows, activity, quarters) for choice in choices],
        "market_totals": _market_totals(view, request["market_id"], quarters, activity["totals"]),
    }


def resolve_csd_market(mart_product_codes: set[str]) -> CsdCrosswalk:
    """Resolve a mart market to exactly one CSD market by product overlap."""

    rows = db.fetch_all(_sql_csd_products())
    by_market: dict[str, set[str]] = {}
    for row in rows:
        by_market.setdefault(str(row["market"]), set()).add(str(row["master_product"]))
    scored: list[CsdCrosswalk] = []
    for market, products in by_market.items():
        overlap = tuple(sorted(normalized_product_overlap(mart_product_codes, products)))
        if overlap:
            scored.append(CsdCrosswalk(market=market, display_market=display_csd_market(market), overlap=overlap, score=len(overlap)))
    if not scored:
        raise CsdTimeseriesAmbiguousMarketError("no CSD market overlaps mart product codes")
    scored.sort(key=lambda item: (-item.score, item.market))
    best = scored[0]
    ties = [item for item in scored if item.score == best.score]
    if len(ties) > 1:
        raise CsdTimeseriesAmbiguousMarketError(f"CSD market overlap tie: {', '.join(item.market for item in ties)}")
    return best


def _parse_request(payload: Mapping[str, Any]) -> JsonMap:
    view = text(payload.get("view"))
    if view not in {"general", "strategic_ml"}:
        raise CsdTimeseriesInputError(f"unsupported view: {view}")
    market_id = text(payload.get("market_id"))
    selected_brand = text(payload.get("selected_brand"))
    if not market_id or not selected_brand:
        raise CsdTimeseriesInputError("market_id and selected_brand are required")
    filter_payload = payload.get("filter")
    window = payload.get("window")
    return {
        "view": view,
        "market_id": market_id,
        "selected_brand": selected_brand,
        "filter": filter_payload if isinstance(filter_payload, dict) else {},
        "window": window if isinstance(window, dict) else {},
    }


def _view_config(view: str) -> ViewConfig:
    if view == "general":
        return ViewConfig("mart_general_brand_metric", "mart_general_market_metric", "atc4_code", "atc4_desc", "brand_ranking", False)
    return ViewConfig("mart_strategic_ml_brand_metric", "mart_strategic_ml_market_metric", "ml_id", "ml_name", "brand_ranking_stacked", True)


def _scope_payload(request: JsonMap, view: ViewConfig, market_row: JsonMap, selected_meta: BrandMeta, ranking: JsonMap, crosswalk: CsdCrosswalk, quarters: list[str]) -> JsonMap:
    return {
        "view": request["view"],
        "market_id": request["market_id"],
        "market_name": str(market_row.get(view.market_name_column) or request["market_id"]),
        "csd_market": crosswalk.display_market,
        "selected_brand": {"brand_key": selected_meta.brand_key, "product_code": first(selected_meta.product_codes)},
        "ranking_measure": RANKING_MEASURE,
        "ranking_quarter": ranking["quarter"],
        "filter": request["filter"],
        "quarters": quarters,
        "measures": list(PUBLIC_MEASURES),
    }


def _fetch_brand_rows(view: ViewConfig, market_id: str) -> list[JsonMap]:
    is_jw = "is_jw" if view.has_is_jw else "0 AS is_jw"
    return db.fetch_all(
        f"""
        SELECT DISTINCT brand_key, brand_name, {is_jw}, by_dimension, metric_history
        FROM {quote_identifier(config.db_name)}.{quote_identifier(view.brand_table)}
        WHERE {view.market_key} = %s AND source = %s AND measure = %s
        ORDER BY brand_key
        """,
        (market_id, SOURCE, RANKING_MEASURE),
    )


def _fetch_market_row(view: ViewConfig, market_id: str, measure: str) -> JsonMap | None:
    return db.fetch_one(
        f"""
        SELECT {view.market_key}, {view.market_name_column}, market_size_series, {view.ranking_column}
        FROM {quote_identifier(config.db_name)}.{quote_identifier(view.market_table)}
        WHERE {view.market_key} = %s AND source = %s AND measure = %s
        LIMIT 1
        """,
        (market_id, SOURCE, measure),
    )


def _fetch_rx_rows(view: ViewConfig, market_id: str, brand_keys: tuple[str, ...]) -> list[JsonMap]:
    if not brand_keys:
        return []
    placeholders = ", ".join(["%s"] * len(brand_keys))
    return db.fetch_all(
        f"""
        SELECT brand_key, measure, raw_value_history, metric_history
        FROM {quote_identifier(config.db_name)}.{quote_identifier(view.brand_table)}
        WHERE {view.market_key} = %s AND source = %s
          AND measure IN (%s, %s, %s) AND brand_key IN ({placeholders})
        """,
        (market_id, SOURCE, *RX_MEASURES, *brand_keys),
    )


def _market_totals(view: ViewConfig, market_id: str, quarters: list[str], activity_totals: JsonMap) -> JsonMap:
    rows = db.fetch_all(
        f"""
        SELECT measure, market_size_series
        FROM {quote_identifier(config.db_name)}.{quote_identifier(view.market_table)}
        WHERE {view.market_key} = %s AND source = %s AND measure IN (%s, %s, %s)
        """,
        (market_id, SOURCE, *RX_MEASURES),
    )
    totals = {"activity": activity_totals}
    for row in rows:
        history = json_map(row.get("market_size_series"))
        totals[str(row["measure"])] = {quarter: float_value(history.get(quarter)) for quarter in quarters}
    for measure in RX_MEASURES:
        totals.setdefault(measure, {quarter: 0.0 for quarter in quarters})
    return totals


def _activity_series(csd_market: str, choices: list[BrandChoice], metas: dict[str, BrandMeta], quarters: list[str]) -> JsonMap:
    rows = db.fetch_all(_sql_csd_activity(), (csd_market,))
    totals = {quarter: 0.0 for quarter in quarters}
    by_brand = {choice.brand_key: {quarter: 0.0 for quarter in quarters} for choice in choices}
    matched = {choice.brand_key: False for choice in choices}
    code_sets = {key: set(meta.product_codes) for key, meta in metas.items()}
    for row in rows:
        quarter = period_ym_to_quarter(str(row["period_ym"]))
        if quarter not in totals:
            continue
        value = float_value(row.get("value"))
        totals[quarter] += value
        product = normalize_iqvia_en(str(row["master_product"]))
        for brand_key, codes in code_sets.items():
            if brand_key in by_brand and product in codes:
                by_brand[brand_key][quarter] += value
                matched[brand_key] = True
    return {"totals": totals, "by_brand": by_brand, "matched": matched}


def _brand_payload(choice: BrandChoice, metas: dict[str, BrandMeta], rx_rows: list[JsonMap], activity: JsonMap, quarters: list[str]) -> JsonMap:
    meta = metas.get(choice.brand_key, BrandMeta(choice.brand_key, choice.brand_name, (), False))
    series = {"activity": _activity_payload(choice.brand_key, activity, quarters)}
    for measure in RX_MEASURES:
        row = next((item for item in rx_rows if item["brand_key"] == choice.brand_key and item["measure"] == measure), None)
        series[measure] = _rx_payload(row, quarters)
    return {
        "brand_key": choice.brand_key,
        "brand_name": meta.brand_name or choice.brand_name,
        "product_code": first(meta.product_codes),
        "is_selected": choice.is_selected,
        "is_jw": meta.is_jw,
        "sales_rank": choice.sales_rank,
        "csd_matched": bool(activity["matched"].get(choice.brand_key)),
        "series": series,
    }


def _activity_payload(brand_key: str, activity: JsonMap, quarters: list[str]) -> JsonMap:
    absolute = activity["by_brand"].get(brand_key, {quarter: 0.0 for quarter in quarters})
    totals = activity["totals"]
    return {"source": "csd", "absolute": absolute, "ratio": {quarter: ratio(absolute[quarter], totals[quarter]) for quarter in quarters}}


def _rx_payload(row: JsonMap | None, quarters: list[str]) -> JsonMap:
    raw = json_map(row.get("raw_value_history")) if row else {}
    metric = json_map(row.get("metric_history")) if row else {}
    return {
        "source": SOURCE,
        "absolute": {quarter: float_value(raw.get(quarter)) for quarter in quarters},
        "ratio": {quarter: float_value(json_map(metric.get(quarter)).get("ms")) for quarter in quarters},
    }


def _brand_meta_by_key(rows: list[JsonMap], *, has_is_jw: bool) -> dict[str, BrandMeta]:
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


def _ranking_for_quarter(row: JsonMap, ranking_column: str, quarters: list[str]) -> JsonMap:
    ranking = json_map(row.get(ranking_column))
    quarter = next((quarter for quarter in reversed(quarters) if quarter in ranking), sorted(ranking)[-1] if ranking else "")
    items = ranking.get(quarter, [])
    return {"quarter": quarter, "items": items if isinstance(items, list) else []}


def _with_selected_rank(ranking: list[JsonMap], rows: list[JsonMap], selected_brand: str, quarter: str) -> list[JsonMap]:
    if any(item.get("brand_key") == selected_brand for item in ranking):
        return ranking
    selected = next((row for row in rows if row.get("brand_key") == selected_brand), None)
    if selected is None:
        return ranking
    metric = json_map(json_map(selected.get("metric_history")).get(quarter))
    return [{"brand_key": selected_brand, "brand": selected_brand, "rank": int_or_none(metric.get("rank"))}, *ranking]


def _requested_quarters(default_quarters: list[str], window: Mapping[str, Any]) -> list[str]:
    start = text(window.get("start"))
    end = text(window.get("end"))
    return [quarter for quarter in default_quarters if (not start or quarter >= start) and (not end or quarter <= end)]


def _sql_csd_months() -> str:
    return f"SELECT DISTINCT period_ym FROM {quote_identifier(config.brand_activity_db_name)}.`csd_channel_dynamics_stage` ORDER BY period_ym"


def _sql_csd_products() -> str:
    return f"SELECT market, master_product FROM {quote_identifier(config.brand_activity_db_name)}.`csd_channel_dynamics_stage` WHERE jw_channel = 'TOTAL' GROUP BY market, master_product"


def _sql_csd_activity() -> str:
    return f"SELECT period_ym, master_product, SUM(product_details) AS value FROM {quote_identifier(config.brand_activity_db_name)}.`csd_channel_dynamics_stage` WHERE market = %s AND jw_channel = 'TOTAL' GROUP BY period_ym, master_product"
