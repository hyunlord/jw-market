from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en
from pipeline.scripts.api import db
from pipeline.scripts.api.brand_activity_brand_resolver import BrandSetInputError, resolve_brand_set
from pipeline.scripts.api.brand_activity_csd_presence import iqvia_product_codes_by_brand
from pipeline.scripts.api.brand_activity_csd_shared import (
    PUBLIC_MEASURES,
    RANKING_MEASURE,
    RX_MEASURES,
    SOURCE,
    BrandChoice,
    BrandMeta,
    CsdCrosswalk,
    CsdMarketFilterError,
    CsdTimeseriesAmbiguousMarketError,
    CsdTimeseriesInputError,
    CsdTimeseriesNoMappingError,
    JsonMap,
    ViewConfig,
    display_csd_market,
    first,
    float_value,
    full_quarters_from_months,
    json_map,
    months_in_quarter_window,
    normalized_product_overlap,
    ratio,
    text,
)
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.types import quote_identifier


@dataclass(frozen=True, slots=True)
class CsdProductCodes:
    """IQVIA product-code sets used only at the CSD crosswalk boundary."""

    selected: frozenset[str]
    candidates: frozenset[str]
    by_brand: dict[str, frozenset[str]]


def get_csd_timeseries(payload: Mapping[str, Any]) -> JsonMap | None:
    """Return integrated CSD activity and IQVIA prescription series."""

    request = _parse_request(payload)
    all_csd_months = [str(row["period_ym"]) for row in db.fetch_all(_sql_csd_months())]
    quarters = _requested_quarters(full_quarters_from_months(all_csd_months), request["window"])
    if not quarters:
        return None
    activity_months = months_in_quarter_window(all_csd_months, quarters)
    try:
        brand_set = resolve_brand_set(
            view_name=request["view"],
            market_id=request["market_id"],
            selected_brand=request["selected_brand"],
            filter_payload=request["filter"],
            ranking_quarters=quarters,
            prefilter_strategic_choices=True,
        )
    except BrandSetInputError as exc:
        raise CsdTimeseriesInputError(str(exc)) from exc
    if brand_set is None:
        return None
    choices = list(brand_set.choices)
    brand_meta = brand_set.brand_meta
    selected_meta = brand_meta.get(brand_set.selected_brand)
    if selected_meta is None:
        return None
    csd_codes = _iqvia_csd_product_codes(brand_meta, selected_brand=brand_set.selected_brand)
    crosswalks = resolve_csd_markets(
        selected_product_codes=set(csd_codes.selected),
        candidate_product_codes=set(csd_codes.candidates),
    )
    selected_crosswalks = _select_csd_markets(crosswalks, request["csd_market"])
    crosswalk = selected_crosswalks[0]
    rx_rows = _fetch_rx_rows(brand_set.view, brand_set.market_id, tuple(choice.brand_key for choice in choices))
    activity_by_market = {
        item.display_market: _activity_series(item.market, choices, csd_codes.by_brand, activity_months)
        for item in selected_crosswalks
    }
    aggregate = _aggregate_market_activity(activity_by_market)
    activity = activity_by_market[crosswalk.display_market]
    return {
        "scope": _scope_payload(
            request,
            brand_set.view,
            brand_set.market_row,
            selected_meta,
            brand_set.ranking_quarter,
            brand_set.applied_filter,
            crosswalk,
            crosswalks,
            quarters,
            activity_months,
        ),
        "brands": [_brand_payload(choice, brand_meta, rx_rows, activity, quarters, activity_months) for choice in choices],
        "market_totals": _market_totals(brand_set.view, brand_set.market_id, quarters, activity["totals"]),
        "series_by_csd_market": aggregate["series_by_market"],
        "aggregate": {
            "series": aggregate["series"],
            "available": aggregate["available"],
            "contributing_markets_by_period": aggregate["contributing_markets_by_period"],
        },
    }


def _iqvia_csd_product_codes(
    brand_meta: Mapping[str, BrandMeta],
    *,
    selected_brand: str,
) -> CsdProductCodes:
    iqvia_codes = iqvia_product_codes_by_brand(
        {brand_key: meta.brand_name for brand_key, meta in brand_meta.items()}
    )
    by_brand = {brand_key: frozenset(codes) for brand_key, codes in iqvia_codes.items()}
    selected = by_brand.get(selected_brand, frozenset())
    candidates = frozenset(code for codes in by_brand.values() for code in codes)
    return CsdProductCodes(selected=selected, candidates=candidates, by_brand=by_brand)


def resolve_csd_markets(
    *,
    selected_product_codes: set[str],
    candidate_product_codes: set[str],
) -> tuple[CsdCrosswalk, ...]:
    """Resolve CSD markets represented by the selected IQVIA market brand set."""

    scored, selected_markets = _scored_csd_markets(
        selected_product_codes=selected_product_codes,
        candidate_product_codes=candidate_product_codes,
    )
    primary = _primary_csd_market(scored, selected_markets)
    return (
        primary,
        *(
            item
            for item in scored
            if item.market in selected_markets and item.market != primary.market
        ),
    )


def _scored_csd_markets(
    *,
    selected_product_codes: set[str],
    candidate_product_codes: set[str],
) -> tuple[list[CsdCrosswalk], set[str]]:
    rows = db.fetch_all(_sql_csd_products())
    by_market: dict[str, set[str]] = {}
    for row in rows:
        by_market.setdefault(str(row["market"]), set()).add(str(row["master_product"]))
    scored: list[CsdCrosswalk] = []
    selected_markets: set[str] = set()
    for market, products in by_market.items():
        selected_overlap = normalized_product_overlap(selected_product_codes, products)
        overlap = tuple(sorted(normalized_product_overlap(candidate_product_codes, products)))
        if not overlap:
            continue
        if selected_overlap:
            selected_markets.add(market)
        scored.append(
            CsdCrosswalk(
                market=market,
                display_market=display_csd_market(market),
                overlap=overlap,
                score=len(overlap),
            )
        )
    return sorted(scored, key=lambda item: (-item.score, item.market)), selected_markets


def resolve_csd_market(
    *,
    selected_product_codes: set[str],
    candidate_product_codes: set[str],
) -> CsdCrosswalk:
    """Resolve the selected brand to one CSD market, then rank by full overlap."""

    all_scored, selected_markets = _scored_csd_markets(
        selected_product_codes=selected_product_codes,
        candidate_product_codes=candidate_product_codes,
    )
    return _primary_csd_market(all_scored, selected_markets)


def _primary_csd_market(
    scored: list[CsdCrosswalk],
    selected_markets: set[str],
) -> CsdCrosswalk:
    selected = [item for item in scored if item.market in selected_markets]
    if not selected:
        raise CsdTimeseriesNoMappingError("이 브랜드는 CSD 원천에 활동 데이터가 없음")
    best = selected[0]
    ties = [item for item in selected if item.score == best.score]
    if len(ties) > 1:
        raise CsdTimeseriesAmbiguousMarketError(
            f"CSD market overlap tie: {', '.join(item.market for item in ties)}",
            candidates=tuple(_crosswalk_payload(item) for item in ties),
        )
    return best


def _select_csd_markets(
    crosswalks: tuple[CsdCrosswalk, ...],
    requested: str | None,
) -> tuple[CsdCrosswalk, ...]:
    if requested is None:
        return crosswalks
    normalized = requested.strip().casefold()
    selected = tuple(
        item
        for item in crosswalks
        if normalized in {item.market.casefold(), item.display_market.casefold()}
    )
    if selected:
        return selected
    raise CsdMarketFilterError(requested, available=tuple(item.display_market for item in crosswalks))


def _crosswalk_payload(item: CsdCrosswalk) -> JsonMap:
    return {
        "market": item.market,
        "display_market": item.display_market,
        "overlap": list(item.overlap),
        "score": item.score,
    }


def _parse_request(payload: Mapping[str, Any]) -> JsonMap:
    view = text(payload.get("view"))
    if view not in {"general", "strategic_ml", "strategic_cd"}:
        raise CsdTimeseriesInputError(f"unsupported view: {view}")
    selected_brand = text(payload.get("selected_brand"))
    filter_payload = _filter_payload(payload)
    market_id = (_first_filter_value(filter_payload, "atc4") or None) if view == "general" else (text(payload.get("market_id")) or None)
    if not selected_brand or (view == "general" and not market_id and not _has_market_scope(filter_payload)):
        raise CsdTimeseriesInputError("filters.atc4 and selected_brand are required")
    window = payload.get("window")
    return {
        "view": view,
        "market_id": market_id,
        "selected_brand": selected_brand,
        "csd_market": text(payload.get("csd_market")).strip() or None,
        "filter": filter_payload,
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
    crosswalk: CsdCrosswalk,
    crosswalks: tuple[CsdCrosswalk, ...],
    quarters: list[str],
    activity_months: tuple[str, ...],
) -> JsonMap:
    return {
        "view": request["view"],
        "market_id": str(market_row.get(view.market_key) or request["market_id"]),
        "market_name": str(market_row.get(view.market_name_column) or market_row.get(view.market_key) or request["market_id"]),
        "csd_market": crosswalk.display_market,
        "csd_markets": [item.display_market for item in crosswalks],
        "selected_brand": {"brand_key": selected_meta.brand_key, "product_code": first(selected_meta.product_codes)},
        "ranking_measure": RANKING_MEASURE,
        "ranking_quarter": ranking_quarter,
        "filter": request["filter"],
        "applied_filter": applied_filter,
        "applied_filters": applied_filter,
        "filter_effect": {
            "brand_set": "channel_axis_applied" if applied_filter.get("channel_axis") else "base",
            "activity": "csd_total_channel",
            "rx": "iqvia_nsa_public_measures",
        },
        "resolved_market": _resolved_market_payload(request, view, market_row),
        "quarters": quarters,
        "activity_months": list(activity_months),
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


def _has_market_scope(filter_payload: Mapping[str, Any]) -> bool:
    return isinstance(filter_payload.get("market_scope"), Mapping)


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
    measure_placeholders = ", ".join(["%s"] * len(RX_MEASURES))
    placeholders = ", ".join(["%s"] * len(brand_keys))
    return db.fetch_all(
        f"""
        SELECT brand_key, measure, raw_value_history, metric_history
        FROM {quote_identifier(config.db_name)}.{quote_identifier(view.brand_table)}
        WHERE {view.market_key} = %s AND source = %s
          AND measure IN ({measure_placeholders}) AND brand_key IN ({placeholders})
        """,
        (market_id, SOURCE, *RX_MEASURES, *brand_keys),
    )


def _market_totals(view: ViewConfig, market_id: str, quarters: list[str], activity_totals: JsonMap) -> JsonMap:
    measure_placeholders = ", ".join(["%s"] * len(RX_MEASURES))
    rows = db.fetch_all(
        f"""
        SELECT measure, market_size_series
        FROM {quote_identifier(config.db_name)}.{quote_identifier(view.market_table)}
        WHERE {view.market_key} = %s AND source = %s AND measure IN ({measure_placeholders})
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


def _activity_series(
    csd_market: str,
    choices: list[BrandChoice],
    product_codes_by_brand: Mapping[str, frozenset[str]],
    activity_months: tuple[str, ...],
) -> JsonMap:
    rows = db.fetch_all(_sql_csd_activity(), (csd_market,))
    totals = {month: 0.0 for month in activity_months}
    by_brand = {choice.brand_key: {month: 0.0 for month in activity_months} for choice in choices}
    matched = {choice.brand_key: False for choice in choices}
    observed_months: set[str] = set()
    for row in rows:
        month = str(row["period_ym"])
        if month not in totals:
            continue
        observed_months.add(month)
        value = float_value(row.get("value"))
        totals[month] += value
        product = normalize_iqvia_en(str(row["master_product"]))
        for brand_key, codes in product_codes_by_brand.items():
            if brand_key in by_brand and product in codes:
                by_brand[brand_key][month] += value
                matched[brand_key] = True
    return {
        "totals": totals,
        "by_brand": by_brand,
        "matched": matched,
        "observed_months": tuple(sorted(observed_months)),
    }


def _aggregate_market_activity(activity_by_market: Mapping[str, JsonMap]) -> JsonMap:
    totals: dict[str, float] = {}
    by_entity: dict[str, dict[str, float]] = {}
    contributors: dict[str, list[str]] = {}
    available: dict[str, JsonMap] = {}
    series_by_market: dict[str, JsonMap] = {}
    for market, activity in activity_by_market.items():
        observed = activity.get("observed_months")
        periods = tuple(str(period) for period in (observed if observed is not None else activity["totals"].keys()))
        if periods:
            available[market] = {"start": periods[0], "end": periods[-1]}
        market_totals = {period: float_value(activity["totals"].get(period)) for period in periods}
        market_entities = {
            entity: {period: float_value(values.get(period)) for period in periods}
            for entity, values in activity["by_brand"].items()
        }
        series_by_market[market] = {
            "available": available.get(market),
            "market_totals": market_totals,
            "by_entity": market_entities,
        }
        for period, value in market_totals.items():
            totals[period] = totals.get(period, 0.0) + value
            contributors.setdefault(period, []).append(market)
        for entity, values in market_entities.items():
            target = by_entity.setdefault(entity, {})
            for period, value in values.items():
                target[period] = target.get(period, 0.0) + value
    return {
        "series": {
            "market_totals": dict(sorted(totals.items())),
            "by_entity": {key: dict(sorted(values.items())) for key, values in by_entity.items()},
        },
        "series_by_market": series_by_market,
        "available": available,
        "contributing_markets_by_period": {period: markets for period, markets in sorted(contributors.items())},
    }


def _brand_payload(
    choice: BrandChoice,
    metas: dict[str, BrandMeta],
    rx_rows: list[JsonMap],
    activity: JsonMap,
    quarters: list[str],
    activity_months: tuple[str, ...],
) -> JsonMap:
    meta = metas.get(choice.brand_key, BrandMeta(choice.brand_key, choice.brand_name, (), False))
    series = {"activity": _activity_payload(choice.brand_key, activity, activity_months)}
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


def _activity_payload(brand_key: str, activity: JsonMap, activity_months: tuple[str, ...]) -> JsonMap:
    absolute = activity["by_brand"].get(brand_key, {month: 0.0 for month in activity_months})
    totals = activity["totals"]
    return {"source": "csd", "absolute": absolute, "ratio": {month: ratio(absolute[month], totals[month]) for month in activity_months}}


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
