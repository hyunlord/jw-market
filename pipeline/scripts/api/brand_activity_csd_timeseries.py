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
    csd_markets_for_products,
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
    # Channel Dynamics market axis = the CSD Market sheets of *every brand in the
    # resolved market* (MI Master grouping), not just the selected brand.  A family
    # such as 리바로 + 리바로젯 shares one strategic market (ml_006) but sits in two
    # separate workbook sheets (LIVALO Market / LIVALOZET Market); scoping to the
    # selected brand's own product codes dropped the sibling sheet.  The union over
    # market members restores both sheets; the selected brand's own sheet(s) stay
    # primary so the backward-compatible ``csd_market`` label is unchanged.
    member_product_codes = {code for meta in brand_meta.values() for code in meta.product_codes}
    available_markets = _order_selected_first(
        resolve_csd_markets(member_product_codes), set(selected_meta.product_codes)
    )
    active_markets, selected_market_key = _select_active_markets(available_markets, request["csd_market"])
    rx_rows = _fetch_rx_rows(brand_set.view, brand_set.market_id, tuple(choice.brand_key for choice in choices))
    activity = _activity_series([market.market for market in active_markets], choices, brand_meta, quarters)
    return {
        "scope": _scope_payload(
            request,
            brand_set.view,
            brand_set.market_row,
            selected_meta,
            brand_set.ranking_quarter,
            brand_set.applied_filter,
            available_markets,
            selected_market_key,
            quarters,
        ),
        "brands": [_brand_payload(choice, brand_meta, rx_rows, activity, quarters) for choice in choices],
        "market_totals": _market_totals(brand_set.view, brand_set.market_id, quarters, activity["totals"]),
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


# Tokens that mean "aggregate every sheet the brand belongs to" (시장 전체 default).
_ALL_MARKET_TOKENS = frozenset({"전체", "all", "total"})


def resolve_csd_markets(product_codes: set[str]) -> tuple[CsdCrosswalk, ...]:
    """Return all CSD Market sheets a brand's products overlap (dropdown source).

    Unlike :func:`resolve_csd_market` this never raises on ambiguity — a brand in
    multiple workbook Market sheets yields all of them so the UI can offer them.
    """

    rows = db.fetch_all(_sql_csd_products())
    return csd_markets_for_products(product_codes, rows)


def _order_selected_first(
    markets: tuple[CsdCrosswalk, ...], selected_product_codes: set[str]
) -> tuple[CsdCrosswalk, ...]:
    """Keep the selected brand's own CSD Market sheet(s) as the leading (primary) entries.

    ``resolve_csd_markets`` ranks purely by overlap size.  Once the market axis is
    widened to every member brand, a larger sibling sheet (e.g. LIVALOZET) could
    otherwise displace the selected brand's own sheet (e.g. LIVALO) from the primary
    slot used for the backward-compatible ``csd_market`` label.  Sheets overlapping
    the selected brand's products lead — preserving their relative order — and the
    remaining member sheets follow in their existing order.  Both ``selected_product_codes``
    and ``CsdCrosswalk.overlap`` are already IQVIA-normalized, so a plain set
    intersection is sufficient.
    """

    if not selected_product_codes:
        return markets
    own = tuple(market for market in markets if selected_product_codes & set(market.overlap))
    rest = tuple(market for market in markets if not (selected_product_codes & set(market.overlap)))
    return own + rest


def _select_active_markets(
    available: tuple[CsdCrosswalk, ...], requested: str | None
) -> tuple[tuple[CsdCrosswalk, ...], str | None]:
    """Choose the aggregation scope: an explicit sheet, else all sheets (시장 전체).

    Returns ``(active_markets, selected_market_key)`` where ``selected_market_key``
    is ``None`` for the all-sheets default.  An unrecognized request falls back to
    all sheets rather than erroring.
    """

    available = tuple(available)
    if requested:
        key = requested.strip()
        if key and key.lower() not in _ALL_MARKET_TOKENS:
            match = next((item for item in available if item.market == key or item.display_market == key), None)
            if match is not None:
                return ((match,), match.market)
    return (available, None)


def _parse_request(payload: Mapping[str, Any]) -> JsonMap:
    view = text(payload.get("view"))
    if view not in {"general", "strategic_ml"}:
        raise CsdTimeseriesInputError(f"unsupported view: {view}")
    market_id = text(payload.get("market_id"))
    selected_brand = text(payload.get("selected_brand"))
    filter_payload = _filter_payload(payload)
    if view == "general" and not market_id:
        market_id = _first_filter_value(filter_payload, "atc4")
    if not selected_brand or (view == "general" and not market_id):
        raise CsdTimeseriesInputError("market_id or filters.atc4, and selected_brand are required")
    window = payload.get("window")
    return {
        "view": view,
        "market_id": market_id,
        "selected_brand": selected_brand,
        "filter": filter_payload,
        "csd_market": text(payload.get("csd_market")),
        "mode": text(payload.get("mode")) or "absolute",
        "window": window if isinstance(window, dict) else {},
    }


def _scope_payload(
    request: JsonMap,
    view: ViewConfig,
    market_row: JsonMap,
    selected_meta: BrandMeta,
    ranking_quarter: str,
    applied_filter: JsonMap,
    available_markets: tuple[CsdCrosswalk, ...],
    selected_market_key: str | None,
    quarters: list[str],
) -> JsonMap:
    if selected_market_key:
        active_display = next((market.display_market for market in available_markets if market.market == selected_market_key), "")
    else:
        active_display = available_markets[0].display_market if available_markets else ""
    return {
        "view": request["view"],
        "market_id": str(market_row.get(view.market_key) or request["market_id"]),
        "market_name": str(market_row.get(view.market_name_column) or market_row.get(view.market_key) or request["market_id"]),
        # csd_market keeps the currently-scoped market label (backward compatible);
        # csd_markets is the corrected dropdown of the brand's CSD Market sheets;
        # selected_csd_market is the active sheet (None == 시장 전체 / all sheets).
        "csd_market": active_display,
        "csd_markets": [
            {"market": market.market, "label": market.display_market, "score": market.score}
            for market in available_markets
        ],
        "selected_csd_market": selected_market_key,
        "selected_brand": {"brand_key": selected_meta.brand_key, "product_code": first(selected_meta.product_codes)},
        "ranking_measure": RANKING_MEASURE,
        "ranking_quarter": ranking_quarter,
        "filter": request["filter"],
        "applied_filter": applied_filter,
        "applied_filters": applied_filter,
        "resolved_market": _resolved_market_payload(request, view, market_row),
        "quarters": quarters,
        "measures": list(PUBLIC_MEASURES),
        "mode": request["mode"],
    }


def _filter_payload(payload: Mapping[str, Any]) -> JsonMap:
    filters = payload.get("filters")
    legacy_filter = payload.get("filter")
    if isinstance(filters, dict) and filters:
        return filters
    return legacy_filter if isinstance(legacy_filter, dict) else {}


def _first_filter_value(filter_payload: Mapping[str, Any], key: str) -> str:
    value = filter_payload.get(key)
    if isinstance(value, list):
        return text(value[0]) if value else ""
    return text(value)


def _resolved_market_payload(request: JsonMap, view: ViewConfig, market_row: JsonMap) -> JsonMap:
    market_id = str(market_row.get(view.market_key) or request["market_id"])
    return {
        "type": request["view"],
        "market_id": market_id,
        "market_label": str(market_row.get(view.market_name_column) or market_id),
        "source": "filters" if request["view"] == "general" else f"brand:{request['selected_brand']}",
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


def _activity_series(csd_markets: list[str], choices: list[BrandChoice], metas: dict[str, BrandMeta], quarters: list[str]) -> JsonMap:
    totals = {quarter: 0.0 for quarter in quarters}
    by_brand = {choice.brand_key: {quarter: 0.0 for quarter in quarters} for choice in choices}
    matched = {choice.brand_key: False for choice in choices}
    if not csd_markets:
        return {"totals": totals, "by_brand": by_brand, "matched": matched}
    rows = db.fetch_all(_sql_csd_activity(len(csd_markets)), tuple(csd_markets))
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


def _sql_csd_activity(market_count: int) -> str:
    placeholders = ", ".join(["%s"] * market_count)
    return f"SELECT period_ym, master_product, SUM(product_details) AS value FROM {quote_identifier(config.brand_activity_db_name)}.`csd_channel_dynamics_stage` WHERE market IN ({placeholders}) AND jw_channel = 'TOTAL' GROUP BY period_ym, master_product"
