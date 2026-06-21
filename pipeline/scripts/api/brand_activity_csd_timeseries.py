from __future__ import annotations

from typing import Any, Mapping

from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en
from pipeline.scripts.api import db
from pipeline.scripts.api.brand_activity_brand_resolver import BrandSetInputError, resolve_brand_set
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
    json_map,
    normalized_product_overlap,
    period_ym_to_quarter,
    ratio,
    text,
)
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.types import quote_identifier


def get_csd_timeseries(payload: Mapping[str, Any]) -> JsonMap | None:
    """Return integrated CSD activity and IQVIA prescription series."""

    request = _parse_request(payload)
    all_csd_months = [str(row["period_ym"]) for row in db.fetch_all(_sql_csd_months())]
    quarters = _requested_quarters(full_quarters_from_months(all_csd_months), request["window"])
    if not quarters:
        return None
    try:
        brand_set = resolve_brand_set(
            view_name=request["view"],
            market_id=request["market_id"],
            selected_brand=request["selected_brand"],
            filter_payload=request["filter"],
            ranking_quarters=quarters,
        )
    except BrandSetInputError as exc:
        raise CsdTimeseriesInputError(str(exc)) from exc
    if brand_set is None:
        return None
    choices = list(brand_set.choices)
    brand_meta = brand_set.brand_meta
    selected_meta = brand_meta.get(request["selected_brand"])
    if selected_meta is None:
        return None
    mart_codes = {code for meta in brand_meta.values() for code in meta.product_codes}
    crosswalk = resolve_csd_market(mart_codes)
    rx_rows = _fetch_rx_rows(brand_set.view, request["market_id"], tuple(choice.brand_key for choice in choices))
    activity = _activity_series(crosswalk.market, choices, brand_meta, quarters)
    return {
        "scope": _scope_payload(request, brand_set.view, brand_set.market_row, selected_meta, brand_set.ranking_quarter, brand_set.applied_filter, crosswalk, quarters),
        "brands": [_brand_payload(choice, brand_meta, rx_rows, activity, quarters) for choice in choices],
        "market_totals": _market_totals(brand_set.view, request["market_id"], quarters, activity["totals"]),
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


def _scope_payload(
    request: JsonMap,
    view: ViewConfig,
    market_row: JsonMap,
    selected_meta: BrandMeta,
    ranking_quarter: str,
    applied_filter: JsonMap,
    crosswalk: CsdCrosswalk,
    quarters: list[str],
) -> JsonMap:
    return {
        "view": request["view"],
        "market_id": request["market_id"],
        "market_name": str(market_row.get(view.market_name_column) or request["market_id"]),
        "csd_market": crosswalk.display_market,
        "selected_brand": {"brand_key": selected_meta.brand_key, "product_code": first(selected_meta.product_codes)},
        "ranking_measure": RANKING_MEASURE,
        "ranking_quarter": ranking_quarter,
        "filter": request["filter"],
        "applied_filter": applied_filter,
        "quarters": quarters,
        "measures": list(PUBLIC_MEASURES),
    }


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
