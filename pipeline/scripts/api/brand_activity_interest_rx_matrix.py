from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en
from pipeline.scripts.api.brand_activity_brand_resolver import BrandSetInputError, BrandSetResolution, resolve_brand_set
from pipeline.scripts.api.brand_activity_csd_shared import (
    BrandChoice,
    BrandMeta,
    CsdCrosswalk,
    CsdTimeseriesAmbiguousMarketError,
    CsdTimeseriesNoMappingError,
    JsonMap,
    first,
    float_value,
    text,
)
from pipeline.scripts.api.brand_activity_csd_timeseries import resolve_csd_market
from pipeline.scripts.api.brand_activity_interest_rx_config import (
    INTEREST_LEVELS,
    RX_EVOLUTION_LEVELS,
    RX_FREQ_LEVELS,
    levels_payload,
    resolved_weights,
)
from pipeline.scripts.api.brand_activity_interest_rx_source import (
    DetailingQuery,
    InterestRxSourceError,
    KeywordQuery,
    PeriodWindow,
    dynamic_period_window,
    fetch_detailing_rows,
    fetch_keyword_rows,
    period_for_request,
)
from pipeline.scripts.api.brand_activity_topic_matrix import _alias_lookup


class InterestRxMatrixInputError(RuntimeError):
    """Raised when an interest/Rx matrix request cannot be parsed."""


@dataclass(frozen=True, slots=True)
class MatrixRequest:
    """Parsed request for the interest/Rx matrix service."""

    view: str
    market_id: str | None
    selected_brand: str
    filter_payload: JsonMap
    visit_location: str
    specialty: str
    period_start: str
    period_end: str
    weights: JsonMap


@dataclass(frozen=True, slots=True)
class MatrixInputs:
    """Resolved entities needed to project the response."""

    request: MatrixRequest
    period: PeriodWindow
    brand_set: BrandSetResolution
    weights: JsonMap
    aliases: dict[str, str]
    crosswalk: CsdCrosswalk | None
    csd_availability: JsonMap | None


@dataclass(frozen=True, slots=True)
class Projection:
    """Computed aggregations ready for JSON projection."""

    inputs: MatrixInputs
    brand_counts: dict[str, JsonMap]
    detailing: dict[str, float | None]


def get_interest_rx_matrix(payload: Mapping[str, Any]) -> JsonMap | None:
    """Return selected and competitor interest/Rx distributions with detailing."""

    request = _parse_request(payload)
    period = _period(request)
    try:
        brand_set = resolve_brand_set(
            view_name=request.view,
            market_id=request.market_id,
            selected_brand=request.selected_brand,
            filter_payload=request.filter_payload,
        )
    except BrandSetInputError as exc:
        raise InterestRxMatrixInputError(str(exc)) from exc
    if brand_set is None:
        return None
    inputs = _inputs(request, period, brand_set)
    keyword_rows = fetch_keyword_rows(_keyword_query(inputs))
    projection = Projection(inputs, _counts_by_brand(inputs, keyword_rows), _detailing_by_brand(inputs))
    return {
        "scope": _scope_payload(inputs),
        "filters_applied": _filters_payload(inputs),
        "period": _period_payload(period),
        "levels": levels_payload(),
        "weights": inputs.weights,
        "brands": [_brand_payload(choice, projection) for choice in brand_set.choices],
        "market_average": _stats_payload(_counts_from_rows(keyword_rows), inputs.weights),
    }


def _parse_request(payload: Mapping[str, Any]) -> MatrixRequest:
    view = text(payload.get("view"))
    if view not in {"general", "strategic_ml", "strategic_cd"}:
        raise InterestRxMatrixInputError(f"unsupported view: {view}")
    selected_brand = text(payload.get("selected_brand"))
    filter_payload = _filter_payload(payload)
    market_id = (_first_filter_value(filter_payload, "atc4") or None) if view == "general" else (text(payload.get("market_id")) or None)
    if not selected_brand or (view == "general" and not market_id and not _has_market_scope(filter_payload)):
        raise InterestRxMatrixInputError("filters.atc4 and selected_brand are required")
    weights = payload.get("weights")
    return MatrixRequest(
        view=view,
        market_id=market_id,
        selected_brand=selected_brand,
        filter_payload=filter_payload,
        visit_location=text(payload.get("visit_location")) or "전체",
        specialty=text(payload.get("specialty")) or "전체",
        period_start=text(payload.get("period_start")),
        period_end=text(payload.get("period_end")),
        weights=weights if isinstance(weights, dict) else {},
    )


def _period(request: MatrixRequest) -> PeriodWindow:
    try:
        return period_for_request(request.period_start, request.period_end, dynamic_period_window())
    except InterestRxSourceError as exc:
        raise InterestRxMatrixInputError(str(exc)) from exc


def _inputs(request: MatrixRequest, period: PeriodWindow, brand_set: BrandSetResolution) -> MatrixInputs:
    aliases = _alias_lookup()
    selected_meta = brand_set.brand_meta[brand_set.selected_brand]
    candidate_codes = {
        code
        for choice in brand_set.choices
        for code in brand_set.brand_meta[choice.brand_key].product_codes
    }
    crosswalk, csd_availability = _maybe_csd_market(
        selected_product_codes=set(selected_meta.product_codes),
        candidate_product_codes=candidate_codes,
    )
    return MatrixInputs(request, period, brand_set, resolved_weights(request.weights), aliases, crosswalk, csd_availability)


def _maybe_csd_market(
    *,
    selected_product_codes: set[str],
    candidate_product_codes: set[str],
) -> tuple[CsdCrosswalk | None, JsonMap | None]:
    try:
        return (
            resolve_csd_market(
                selected_product_codes=selected_product_codes,
                candidate_product_codes=candidate_product_codes,
            ),
            None,
        )
    except CsdTimeseriesNoMappingError as exc:
        return None, _csd_availability("no_csd_mapping", str(exc), csd_source_present=False)
    except CsdTimeseriesAmbiguousMarketError as exc:
        return None, _csd_availability(
            "csd_market_ambiguous",
            str(exc),
            csd_source_present=True,
            candidates=list(exc.candidates),
        )


def _keyword_query(inputs: MatrixInputs) -> KeywordQuery:
    product_codes = tuple(sorted({code for meta in inputs.brand_set.brand_meta.values() for code in meta.product_codes}))
    return KeywordQuery(
        period=inputs.period,
        view=inputs.brand_set.view,
        market_id=inputs.brand_set.market_id,
        product_codes=product_codes,
        visit_location=inputs.request.visit_location,
        specialty=inputs.request.specialty,
    )


def _detailing_by_brand(inputs: MatrixInputs) -> dict[str, float | None]:
    if inputs.crosswalk is None:
        return {choice.brand_key: None for choice in inputs.brand_set.choices}
    rows = fetch_detailing_rows(DetailingQuery(inputs.crosswalk.market, inputs.period))
    result = {choice.brand_key: None for choice in inputs.brand_set.choices}
    code_sets = _code_sets(inputs.brand_set.brand_meta, inputs.aliases)
    for row in rows:
        product = _canonical_product(text(row.get("master_product")), inputs.aliases)
        for choice in inputs.brand_set.choices:
            if product in code_sets.get(choice.brand_key, set()):
                result[choice.brand_key] = (result[choice.brand_key] or 0.0) + float_value(row.get("detailing"))
    return result


def _counts_by_brand(inputs: MatrixInputs, rows: list[JsonMap]) -> dict[str, JsonMap]:
    result = {choice.brand_key: _empty_counts() for choice in inputs.brand_set.choices}
    code_sets = _code_sets(inputs.brand_set.brand_meta, inputs.aliases)
    for row in rows:
        product = _canonical_product(text(row.get("product_name")), inputs.aliases)
        for choice in inputs.brand_set.choices:
            if product in code_sets.get(choice.brand_key, set()):
                _add_counts(result[choice.brand_key], row)
                break
    return result


def _counts_from_rows(rows: list[JsonMap]) -> JsonMap:
    counts = _empty_counts()
    for row in rows:
        _add_counts(counts, row)
    return counts


def _empty_counts() -> JsonMap:
    return {
        "event_count": 0,
        "interest": {level: 0 for level in INTEREST_LEVELS},
        "rx_frequency": {level: 0 for level in RX_FREQ_LEVELS},
        "prescription_evolution": {level: 0 for level in RX_EVOLUTION_LEVELS},
    }


def _add_counts(counts: JsonMap, row: JsonMap) -> None:
    value = int(float_value(row.get("event_count")))
    counts["event_count"] += value
    _add_axis_count(counts["interest"], text(row.get("interest")), value)
    _add_axis_count(counts["rx_frequency"], text(row.get("prescription_frequency")), value)
    _add_axis_count(counts["prescription_evolution"], text(row.get("prescription_evolution")), value)


def _add_axis_count(distribution: dict[str, int], level: str, value: int) -> None:
    if level in distribution:
        distribution[level] += value


def _brand_payload(choice: BrandChoice, projection: Projection) -> JsonMap:
    meta = projection.inputs.brand_set.brand_meta.get(choice.brand_key, BrandMeta(choice.brand_key, choice.brand_name, (), False))
    payload = {
        "brand_key": choice.brand_key,
        "brand_name": meta.brand_name or choice.brand_name,
        "product_code": first(meta.product_codes),
        "is_selected": choice.is_selected,
        "is_jw": meta.is_jw,
        "sales_rank": choice.sales_rank,
        "detailing": projection.detailing.get(choice.brand_key),
    }
    payload.update(_stats_payload(projection.brand_counts[choice.brand_key], projection.inputs.weights))
    return payload


def _stats_payload(counts: JsonMap, weights: JsonMap) -> JsonMap:
    event_count = int(counts["event_count"])
    interest = counts["interest"]
    rx_frequency = counts["rx_frequency"]
    evolution = counts["prescription_evolution"]
    return {
        "interest_distribution": interest,
        "rx_frequency_distribution": rx_frequency,
        "prescription_evolution_distribution": evolution,
        "event_count": event_count,
        "confidence": "sufficient" if event_count >= 5 else "insufficient",
        "interest_score": _score(interest, weights["interest"]),
        "rx_frequency_score": _score(rx_frequency, weights["rx_frequency"]),
        "prescription_evolution_score": _score(evolution, weights["prescription_evolution"]),
    }


def _score(distribution: Mapping[str, int], weights: Mapping[str, float]) -> float | None:
    total = sum(distribution.values())
    if not total:
        return None
    return sum(count * weights.get(level, 0.0) for level, count in distribution.items()) / total


def _code_sets(metas: dict[str, BrandMeta], aliases: dict[str, str]) -> dict[str, set[str]]:
    return {key: {_canonical_product(code, aliases) for code in meta.product_codes} for key, meta in metas.items()}


def _canonical_product(value: str, aliases: dict[str, str]) -> str:
    normalized = normalize_iqvia_en(value)
    return aliases.get(normalized, normalized)


def _scope_payload(inputs: MatrixInputs) -> JsonMap:
    crosswalk = inputs.crosswalk
    scope = {
        "view": inputs.request.view,
        "market_id": inputs.brand_set.market_id,
        "market_name": str(inputs.brand_set.market_row.get(inputs.brand_set.view.market_name_column) or inputs.brand_set.market_id),
        "selected_brand": inputs.request.selected_brand,
        "csd_market": crosswalk.display_market if crosswalk else None,
        "ranking_quarter": inputs.brand_set.ranking_quarter,
        "applied_filter": inputs.brand_set.applied_filter,
        "applied_filters": inputs.brand_set.applied_filter,
        "resolved_market": _resolved_market_payload(inputs),
    }
    if inputs.csd_availability is not None:
        scope["csd_availability"] = inputs.csd_availability
    return scope


def _csd_availability(
    reason: str,
    message: str,
    *,
    csd_source_present: bool,
    candidates: list[JsonMap] | None = None,
) -> JsonMap:
    return {
        "available": False,
        "reason": reason,
        "message": message,
        "csd_source_present": csd_source_present,
        "candidates": candidates or [],
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


def _resolved_market_payload(inputs: MatrixInputs) -> JsonMap:
    market_id = inputs.brand_set.market_id
    return {
        "type": inputs.request.view,
        "market_id": market_id,
        "market_label": str(inputs.brand_set.market_row.get(inputs.brand_set.view.market_name_column) or market_id),
        "source": "filters" if inputs.request.view == "general" else f"brand:{inputs.request.selected_brand}",
    }


def _filters_payload(inputs: MatrixInputs) -> JsonMap:
    return {
        "visit_location": inputs.request.visit_location,
        "specialty": inputs.request.specialty,
        "period_start": inputs.period.start,
        "period_end": inputs.period.end,
    }


def _period_payload(period: PeriodWindow) -> JsonMap:
    return {
        "start": period.start,
        "end": period.end,
        "default_start": period.default_start,
        "default_end": period.default_end,
        "source": period.source,
    }
