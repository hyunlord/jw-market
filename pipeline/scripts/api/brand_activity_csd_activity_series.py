from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en
from pipeline.scripts.api import db
from pipeline.scripts.api.brand_activity_brand_resolver import BrandSetInputError, BrandSetResolution, resolve_brand_set
from pipeline.scripts.api.brand_activity_csd_activity_contract import (
    DEFAULT_QUARTERS,
    MAX_ENTITIES,
    MAX_QUARTERS,
    CsdActivitySeriesInputError,
    CsdEntityLevel,
    ParsedCsdActivityRequest,
    parse_activity_request,
)
from pipeline.scripts.api.brand_activity_csd_shared import (
    BrandMeta,
    CsdCrosswalk,
    CsdTimeseriesAmbiguousMarketError,
    JsonMap,
    float_value,
    full_quarters_from_months,
    json_map,
    period_ym_to_quarter,
    ratio,
    text,
)
from pipeline.scripts.api.brand_activity_csd_timeseries import resolve_csd_market
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.types import quote_identifier


@dataclass(frozen=True, slots=True)
class ActivityRows:
    """Quarterly CSD activity values by product and company."""

    quarters: tuple[str, ...]
    all_quarters: tuple[str, ...]
    totals: dict[str, float]
    by_product: dict[str, dict[str, float]]
    by_company: dict[str, dict[str, float]]


def get_csd_activity_series(payload: Mapping[str, Any]) -> JsonMap | None:
    """Return Section 1 CSD activity volume, share, and rank time series."""

    request = parse_activity_request(payload)
    all_csd_months = [str(row["period_ym"]) for row in db.fetch_all(_sql_csd_months())]
    all_quarters = tuple(full_quarters_from_months(all_csd_months))
    quarters = _requested_quarters(all_quarters, request.period)
    if not quarters:
        return None
    try:
        brand_set = resolve_brand_set(
            view_name=request.view,
            market_id=request.market_id,
            selected_brand=request.selected_brand,
            filter_payload=_brand_set_filter_payload(request),
            ranking_quarters=quarters,
        )
    except BrandSetInputError as exc:
        raise CsdActivitySeriesInputError(str(exc)) from exc
    if brand_set is None:
        return None
    selected_meta = brand_set.brand_meta.get(request.selected_brand)
    if selected_meta is None:
        return None
    crosswalk = resolve_csd_market({code for meta in brand_set.brand_meta.values() for code in meta.product_codes})
    rows = _fetch_activity_rows(crosswalk, request.csd_channel)
    activity = _activity_rows(rows, quarters, all_quarters)
    selected_key = _selected_entity_key(request.entity_level, request.selected_brand, selected_meta, brand_set)
    entity_keys = _entity_keys(request, selected_key, brand_set, activity)
    rank_source = activity.by_company if request.entity_level == "company" else _brand_activity_by_key(brand_set, activity)
    ranks = _ranks_by_quarter(rank_source, quarters)
    values = rank_source
    return {
        "scope": _scope_payload(request, brand_set, crosswalk, quarters),
        "entity_level": request.entity_level,
        "channel": request.csd_channel,
        "top5_basis": request.top5_basis,
        "period": {"quarters": list(quarters), "max_quarters": MAX_QUARTERS, "default_quarters": DEFAULT_QUARTERS},
        "entities": [_entity_payload(key, selected_key, values.get(key, {}), activity.totals, ranks, quarters, brand_set) for key in entity_keys],
        "applied": {"csd_channel": request.csd_channel, "top5_basis": request.top5_basis, "entity_level": request.entity_level},
    }


def _requested_quarters(all_quarters: tuple[str, ...], period: Mapping[str, Any]) -> tuple[str, ...]:
    start = _quarter_text(period.get("start"))
    end = _quarter_text(period.get("end"))
    selected = [quarter for quarter in all_quarters if (not start or quarter >= start) and (not end or quarter <= end)]
    if not selected:
        return ()
    return tuple((selected[-DEFAULT_QUARTERS:] if not start and not end else selected[-MAX_QUARTERS:]))


def _quarter_text(value: Any) -> str:
    raw = text(value).strip().upper()
    if len(raw) == 6 and raw[4] == "Q":
        return f"{raw[:4]}-{raw[4:]}"
    return raw


def _brand_set_filter_payload(request: ParsedCsdActivityRequest) -> JsonMap:
    if request.top5_basis == "iqvia_sales":
        return request.filter_payload
    return {key: value for key, value in request.filter_payload.items() if key != "channel_axis"}


def _fetch_activity_rows(crosswalk: CsdCrosswalk, csd_channel: str) -> list[JsonMap]:
    return db.fetch_all(_sql_csd_activity(), (crosswalk.market, csd_channel))


def _activity_rows(rows: list[JsonMap], quarters: tuple[str, ...], all_quarters: tuple[str, ...]) -> ActivityRows:
    totals = {quarter: 0.0 for quarter in quarters}
    by_product: dict[str, dict[str, float]] = {}
    by_company: dict[str, dict[str, float]] = {}
    for row in rows:
        quarter = period_ym_to_quarter(str(row["period_ym"]))
        if quarter not in all_quarters:
            continue
        product = normalize_iqvia_en(str(row["master_product"]))
        company = str(row["representing_company"])
        value = float_value(row.get("value"))
        _add_value(by_product, product, quarter, value)
        _add_value(by_company, company, quarter, value)
        if quarter in totals:
            totals[quarter] += value
    return ActivityRows(quarters=quarters, all_quarters=all_quarters, totals=totals, by_product=by_product, by_company=by_company)


def _entity_keys(request: ParsedCsdActivityRequest, selected_key: str, brand_set: BrandSetResolution, activity: ActivityRows) -> tuple[str, ...]:
    if request.selected_entities:
        return request.selected_entities
    ranked = _basis_keys(request, brand_set, activity)
    ordered = [selected_key, *(key for key in ranked if key != selected_key)]
    return tuple(_unique(ordered)[:MAX_ENTITIES])


def _basis_keys(request: ParsedCsdActivityRequest, brand_set: BrandSetResolution, activity: ActivityRows) -> tuple[str, ...]:
    if request.top5_basis == "iqvia_sales":
        return _iqvia_sales_keys(request.entity_level, brand_set)
    values = activity.by_company if request.entity_level == "company" else _brand_activity_by_key(brand_set, activity)
    basis_quarters = activity.quarters if request.top5_basis == "activity_count" else activity.all_quarters
    return tuple(key for key, _value in _ranked_totals(values, tuple(basis_quarters)))


def _iqvia_sales_keys(entity_level: CsdEntityLevel, brand_set: BrandSetResolution) -> tuple[str, ...]:
    if entity_level == "brand":
        return tuple(choice.brand_key for choice in brand_set.choices)
    return tuple(_unique(_company_for_brand(choice.brand_key, brand_set) for choice in brand_set.choices))


def _brand_activity_by_key(brand_set: BrandSetResolution, activity: ActivityRows) -> dict[str, dict[str, float]]:
    values: dict[str, dict[str, float]] = dict(activity.by_product)
    for brand_key, meta in brand_set.brand_meta.items():
        merged: dict[str, float] = {}
        for product in meta.product_codes:
            for quarter, value in activity.by_product.get(normalize_iqvia_en(product), {}).items():
                merged[quarter] = merged.get(quarter, 0.0) + value
        if merged:
            values[brand_key] = merged
    return values


def _selected_entity_key(entity_level: CsdEntityLevel, selected_brand: str, selected_meta: BrandMeta, brand_set: BrandSetResolution) -> str:
    if entity_level == "company":
        return _company_for_brand(selected_brand, brand_set)
    return selected_brand or normalize_iqvia_en(selected_meta.product_codes[0])


def _company_for_brand(brand_key: str, brand_set: BrandSetResolution) -> str:
    row = next((item for item in brand_set.brand_rows if str(item.get("brand_key")) == brand_key), {})
    company = text(json_map(row.get("by_dimension")).get("company")) or text(json_map(row.get("by_dimension")).get("manufacturer"))
    return company or brand_key


def _entity_payload(key: str, selected_key: str, values: dict[str, float], totals: dict[str, float], ranks: dict[str, dict[str, int]], quarters: tuple[str, ...], brand_set: BrandSetResolution) -> JsonMap:
    return {
        "key": key,
        "display_name": key,
        "is_selected": key == selected_key,
        "is_jw": _is_jw(key, brand_set),
        "activity": {
            "absolute": [{"period": quarter, "value": values.get(quarter, 0.0)} for quarter in quarters],
            "share_pct": [{"period": quarter, "value": ratio(values.get(quarter, 0.0), totals.get(quarter, 0.0))} for quarter in quarters],
            "rank": [{"period": quarter, "value": ranks.get(quarter, {}).get(key)} for quarter in quarters],
        },
    }


def _is_jw(key: str, brand_set: BrandSetResolution) -> bool:
    if key in brand_set.brand_meta:
        return brand_set.brand_meta[key].is_jw
    return any(meta.is_jw and _company_for_brand(brand_key, brand_set) == key for brand_key, meta in brand_set.brand_meta.items())


def _ranks_by_quarter(values: dict[str, dict[str, float]], quarters: tuple[str, ...]) -> dict[str, dict[str, int]]:
    ranks: dict[str, dict[str, int]] = {}
    for quarter in quarters:
        ranked = sorted(((key, series.get(quarter, 0.0)) for key, series in values.items()), key=lambda item: (-item[1], item[0]))
        ranks[quarter] = {key: index + 1 for index, (key, _value) in enumerate(ranked) if _value > 0.0}
    return ranks


def _ranked_totals(values: dict[str, dict[str, float]], quarters: tuple[str, ...]) -> list[tuple[str, float]]:
    totals = [(key, sum(series.get(quarter, 0.0) for quarter in quarters)) for key, series in values.items()]
    return sorted((item for item in totals if item[1] > 0.0), key=lambda item: (-item[1], item[0]))


def _scope_payload(request: ParsedCsdActivityRequest, brand_set: BrandSetResolution, crosswalk: CsdCrosswalk, quarters: tuple[str, ...]) -> JsonMap:
    return {
        "view": request.view,
        "market_id": brand_set.market_id,
        "market_name": str(brand_set.market_row.get(brand_set.view.market_name_column) or brand_set.market_id),
        "csd_market": crosswalk.display_market,
        "selected_brand": request.selected_brand,
        "filter": request.filter_payload,
        "applied_filter": brand_set.applied_filter,
        "quarters": list(quarters),
    }


def _unique(values: list[str] | tuple[str, ...] | Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _add_value(target: dict[str, dict[str, float]], key: str, quarter: str, value: float) -> None:
    bucket = target.setdefault(key, {})
    bucket[quarter] = bucket.get(quarter, 0.0) + value


def _sql_csd_months() -> str:
    return f"SELECT DISTINCT period_ym FROM {quote_identifier(config.brand_activity_db_name)}.`csd_channel_dynamics_stage` ORDER BY period_ym"


def _sql_csd_activity() -> str:
    return f"""
        SELECT period_ym, master_product, representing_company, SUM(product_details) AS value
        FROM {quote_identifier(config.brand_activity_db_name)}.`csd_channel_dynamics_stage`
        WHERE market = %s AND jw_channel = %s
        GROUP BY period_ym, master_product, representing_company
    """
