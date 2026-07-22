from __future__ import annotations

from typing import Never

from fastapi import APIRouter, HTTPException, Path, Query

from pipeline.scripts.api.brand_activity_csd_timeseries import (
    CsdMarketFilterError,
    CsdTimeseriesAmbiguousMarketError,
    CsdTimeseriesInputError,
    CsdTimeseriesNoMappingError,
    get_csd_timeseries,
)
from pipeline.scripts.api.brand_activity_csd_presence import (
    CsdPresence,
    get_csd_presence,
    get_csd_presences,
)
from pipeline.scripts.api.brand_activity_brand_resolver import BrandSetInputError
from pipeline.scripts.api.brand_activity_csd_activity_series import (
    CsdActivitySeriesInputError,
    get_csd_activity_series,
)
from pipeline.scripts.api.brand_activity_csd_activity_contract import (
    CSD_ACTIVITY_SERIES_EXAMPLE,
    CsdActivitySeriesRequest,
)
from pipeline.scripts.api.brand_activity_interest_rx_matrix import (
    InterestRxMatrixInputError,
    get_interest_rx_matrix,
)
from pipeline.scripts.api.brand_activity_interest_timeseries import (
    InterestTimeseriesInputError,
    get_interest_timeseries,
)
from pipeline.scripts.api.market_filter_atc_options import canonical_atc4_values
from pipeline.scripts.api.brand_activity_topic_matrix import (
    TopicRequestError,
    get_topic_brand_payload,
    get_topic_period_bounds,
)
from pipeline.scripts.api.brand_activity_topics import (
    JsonValue,
    TopicPayloadError,
    get_topic_payload,
    list_topic_payloads,
)
from pipeline.scripts.api.models.brand_activity import (
    BrandActivityInterestRxRequest,
    BrandActivityInterestTimeseriesRequest,
    BrandActivityTopicsRequest,
    CsdTimeseriesRequest,
)
from pipeline.scripts.api.openapi_docs import (
    BRAND_ACTIVITY_CSD_TIMESERIES_REQUEST_EXAMPLE,
    BRAND_ACTIVITY_CSD_TIMESERIES_RESPONSES,
    BRAND_ACTIVITY_FILTER_DESCRIPTION,
    BRAND_ACTIVITY_INTEREST_RX_REQUEST_EXAMPLE,
    BRAND_ACTIVITY_INTEREST_RX_RESPONSES,
    BRAND_ACTIVITY_TAG,
    BRAND_ACTIVITY_TOPICS_REQUEST_EXAMPLE,
    BRAND_ACTIVITY_TOPICS_RESPONSES,
    brand_activity_request_body,
)


router = APIRouter()


# Internal diagnostic/storage endpoints. The portal-facing Brand Activity contract is the POST matrix route below.
TOPIC_DEBUG_INCLUDE_IN_SCHEMA = False


@router.get(
    "/api/brand-activity/csd-presence",
    tags=[BRAND_ACTIVITY_TAG],
    summary="브랜드 CSD 원천 존재 여부",
)
def brand_activity_csd_presence(
    brand: str | None = Query(None, description="확인할 브랜드명."),
    brands: str | None = Query(None, description="쉼표로 구분한 브랜드명. 최대 50개."),
) -> CsdPresence | list[CsdPresence]:
    """Return CSD product-code mapping presence without building activity payloads."""

    if (brand is None) == (brands is None):
        raise HTTPException(status_code=422, detail={"error": "exactly_one_of_brand_or_brands_required"})
    if brand is not None:
        requested = brand.strip()
        if not requested:
            raise HTTPException(status_code=422, detail={"error": "brand_is_empty"})
        return get_csd_presence(requested)

    requested_brands = [item.strip() for item in (brands or "").split(",") if item.strip()]
    if not requested_brands:
        raise HTTPException(status_code=422, detail={"error": "brands_is_empty"})
    if len(requested_brands) > 50:
        raise HTTPException(status_code=422, detail={"error": "too_many_brands", "limit": 50})
    return get_csd_presences(tuple(requested_brands))


@router.get("/api/brand-activity/topics", include_in_schema=TOPIC_DEBUG_INCLUDE_IN_SCHEMA)
def brand_activity_topics() -> dict[str, JsonValue]:
    """Return all Brand Activity topic market payloads."""
    try:
        return {"data": list_topic_payloads()}
    except TopicPayloadError as exc:
        raise HTTPException(status_code=500, detail={"error": "invalid_brand_activity_topic_payload"}) from exc


@router.get("/api/brand-activity/topics/{scope_id}", include_in_schema=TOPIC_DEBUG_INCLUDE_IN_SCHEMA)
def brand_activity_topic(scope_id: str = Path(description="내부 토픽 scope 식별자.")) -> dict[str, JsonValue]:
    """Return one Brand Activity topic market payload."""
    try:
        payload = get_topic_payload(scope_id)
    except TopicPayloadError as exc:
        raise HTTPException(status_code=500, detail={"error": "invalid_brand_activity_topic_payload"}) from exc
    if payload is None:
        return {"data": None, "reason": "scope_not_found", "scope_id": scope_id}
    return {"data": payload}


@router.post(
    "/jw-brand-activity-mock/api/brand-activity/topics",
    include_in_schema=False,
)
@router.post(
    "/api/brand-activity/topics",
    tags=[BRAND_ACTIVITY_TAG],
    summary="브랜드별 토픽 그리드",
    description=(
        "mock `/jw-brand-activity-mock/api/brand-activity/topics`와 대응되는 포탈 공유 API입니다. "
        "브랜드 카드별 event_count, topic_shares, etc_pct, brand_specific_topics를 반환합니다. "
        "각 share_pct는 전체 활동 행 중 해당 토픽과 관련된 행의 비율을 독립적으로 계산하므로, "
        "한 행이 여러 토픽에 포함될 수 있고 topic_shares 합은 100%를 초과할 수 있습니다. "
        "etc_pct는 max(0, 100 - 표시된 top_n 토픽 share_pct 합)으로 계산되는 호환 필드이며, "
        "기타 토픽이나 미분류 행의 비율이 아니고 top_n에 따라 달라집니다. "
        "event_count=0이면 topic_shares는 빈 배열입니다.\n\n"
        + BRAND_ACTIVITY_FILTER_DESCRIPTION
    ),
    response_model=None,
    openapi_extra={
        "requestBody": brand_activity_request_body(
            {
                "visit_location": {"type": ["string", "array"], "items": {"type": "string"}, "description": "키워드 종별 행 필터."},
                "specialty": {"type": ["string", "array"], "items": {"type": "string"}, "description": "키워드 진료과 행 필터."},
                "interest": {"type": ["string", "array"], "items": {"type": "string"}, "description": "키워드 관심도 행 필터."},
                "prescription_evolution": {"type": ["string", "array"], "items": {"type": "string"}, "description": "처방 변화 행 필터."},
                "start_date": {"type": "string", "pattern": "^\\d{4}-(0[1-9]|1[0-2])$", "description": "행 필터 시작월 YYYY-MM."},
                "end_date": {"type": "string", "pattern": "^\\d{4}-(0[1-9]|1[0-2])$", "description": "행 필터 종료월 YYYY-MM."},
                "period_start": {"type": "string", "description": "Legacy 행 필터 시작월 YYYY-MM."},
                "period_end": {"type": "string", "description": "Legacy 행 필터 종료월 YYYY-MM."},
                "top_n": {"type": "integer", "description": "브랜드 카드별 상위 토픽 개수. 1~10으로 clamp됩니다."},
            },
            BRAND_ACTIVITY_TOPICS_REQUEST_EXAMPLE,
        )
    },
    responses=BRAND_ACTIVITY_TOPICS_RESPONSES,
)
def brand_activity_topic_matrix(payload: BrandActivityTopicsRequest) -> dict[str, JsonValue]:
    """Return selected and competitor brand topic shares."""

    service_payload, request_normalized = _portal_service_request(payload)
    try:
        result = get_topic_brand_payload(service_payload)
    except TopicRequestError as exc:
        _raise_brand_set_context_error(exc)
        raise HTTPException(status_code=400, detail={"error": "invalid_brand_activity_topic_request", "message": str(exc)}) from exc
    except TopicPayloadError as exc:
        raise HTTPException(status_code=500, detail={"error": "invalid_brand_activity_topic_payload"}) from exc
    if result is None:
        _raise_market_not_found(payload)
    period_meta = _topic_period_metadata(payload, get_topic_period_bounds())
    response_meta: dict[str, JsonValue] = {"period": period_meta}
    if _period_filter_active(payload) and not _topic_result_has_data(result):
        result = {**result, "brands": []}
        response_meta["reason"] = "no_data_in_period"
    return _success_response(result, request_normalized=request_normalized, metadata=response_meta)


@router.post(
    "/jw-brand-activity-mock/api/brand-activity/csd-timeseries",
    include_in_schema=False,
)
@router.post(
    "/api/brand-activity/csd-timeseries",
    tags=[BRAND_ACTIVITY_TAG],
    summary="활동·처방 추세",
    description=(
        "mock `/jw-brand-activity-mock/api/brand-activity/csd-timeseries`와 대응되는 포탈 공유 API입니다. "
        "CSD 활동량은 `csd_channel_dynamics_stage`의 `jw_channel='TOTAL'`만 사용하므로 화면 관점의 region=TOTAL입니다. "
        "CSD 활동량은 월간축(activity_months)으로, IQVIA 매출/처방 지표(sales/unit/counting_unit/dosage_unit)는 기존 분기축(quarters)으로 정렬됩니다.\n\n"
        + BRAND_ACTIVITY_FILTER_DESCRIPTION
    ),
    response_model=None,
    openapi_extra={
        "requestBody": brand_activity_request_body(
            {
                "csd_market": {"type": "string", "description": "선택 CSD 시장. 미지정 시 전체 시장과 합산."},
                "mode": {"type": "string", "enum": ["absolute", "share"], "default": "absolute"},
                "window": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string", "description": "조회 시작 분기. 예: 2024-Q1."},
                        "end": {"type": "string", "description": "조회 종료 분기. 예: 2025-Q4."},
                    },
                },
            },
            BRAND_ACTIVITY_CSD_TIMESERIES_REQUEST_EXAMPLE,
        )
    },
    responses=BRAND_ACTIVITY_CSD_TIMESERIES_RESPONSES,
)
def brand_activity_csd_timeseries(payload: CsdTimeseriesRequest) -> dict[str, JsonValue]:
    """Return integrated CSD activity and IQVIA prescription timeseries."""

    service_payload, request_normalized = _portal_service_request(payload)
    try:
        result = get_csd_timeseries(service_payload)
    except CsdTimeseriesInputError as exc:
        _raise_brand_set_context_error(exc)
        raise HTTPException(status_code=400, detail={"error": "invalid_csd_timeseries_request", "message": str(exc)}) from exc
    except CsdMarketFilterError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_csd_market", "message": str(exc), "available": list(exc.available)},
        ) from exc
    except CsdTimeseriesNoMappingError as exc:
        return _csd_unavailable_response("no_csd_mapping", str(exc), csd_source_present=False)
    except CsdTimeseriesAmbiguousMarketError as exc:
        return _csd_unavailable_response(
            "csd_market_ambiguous",
            str(exc),
            csd_source_present=True,
            candidates=list(exc.candidates),
        )
    if result is None:
        _raise_market_not_found(payload)
    return _success_response(result, request_normalized=request_normalized)


@router.post(
    "/api/brand-activity/csd-activity-series",
    tags=[BRAND_ACTIVITY_TAG],
    summary="CSD 활동량·비율·순위 추세",
    description=(
        "문서 Section 1 CSD Channeldynamics 시계열 API입니다. "
        "기존 /csd-timeseries와 별도로 CSD jw_channel 선택, 회사축, 월간 활동량 rank series를 제공합니다. "
        "csd_market 미지정 시 매핑된 전체 시장과 기간 union 합산을 반환하고, 지정 시 해당 시장만 반환합니다."
    ),
    response_model=None,
    openapi_extra={"requestBody": brand_activity_request_body({}, CSD_ACTIVITY_SERIES_EXAMPLE)},
)
def brand_activity_csd_activity_series(payload: CsdActivitySeriesRequest) -> dict[str, JsonValue]:
    """Return Section 1 CSD activity volume, share, and rank time series."""

    try:
        result = get_csd_activity_series(_service_payload(payload))
    except CsdActivitySeriesInputError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_csd_activity_series_request", "message": str(exc)}) from exc
    except CsdMarketFilterError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_csd_market", "message": str(exc), "available": list(exc.available)},
        ) from exc
    except CsdTimeseriesNoMappingError as exc:
        return _csd_unavailable_response("no_csd_mapping", str(exc), csd_source_present=False)
    except CsdTimeseriesAmbiguousMarketError as exc:
        return _csd_unavailable_response(
            "csd_market_ambiguous",
            str(exc),
            csd_source_present=True,
            candidates=list(exc.candidates),
        )
    if result is None:
        return {"data": None, "reason": "market_not_found"}
    return {"data": result}


@router.post(
    "/jw-brand-activity-mock/api/brand-activity/interest-rx-matrix",
    include_in_schema=False,
)
@router.post(
    "/api/brand-activity/interest-rx-matrix",
    tags=[BRAND_ACTIVITY_TAG],
    summary="interest×처방빈도 버블",
    description=(
        "mock `/jw-brand-activity-mock/api/brand-activity/interest-rx-matrix`와 대응되는 포탈 공유 API입니다. "
        "X축은 rx_frequency_score, Y축은 prescription_evolution_score, 버블 면적은 event_count입니다. "
        "market_average는 화면의 점선 십자 기준선입니다.\n\n"
        + BRAND_ACTIVITY_FILTER_DESCRIPTION
    ),
    response_model=None,
    openapi_extra={
        "requestBody": brand_activity_request_body(
            {
                "visit_location": {"type": ["string", "array"], "items": {"type": "string"}, "description": "키워드 종별 행 필터."},
                "specialty": {"type": ["string", "array"], "items": {"type": "string"}, "description": "키워드 진료과 행 필터."},
                "period_start": {"type": "string", "description": "조회 시작월 YYYY-MM."},
                "period_end": {"type": "string", "description": "조회 종료월 YYYY-MM."},
                "weights": {"type": "object", "description": "interest/rx_frequency score 가중치."},
            },
            BRAND_ACTIVITY_INTEREST_RX_REQUEST_EXAMPLE,
        )
    },
    responses=BRAND_ACTIVITY_INTEREST_RX_RESPONSES,
)
def brand_activity_interest_rx_matrix(payload: BrandActivityInterestRxRequest) -> dict[str, JsonValue]:
    """Return interest/Rx distributions and detailing for selected brands."""

    service_payload, request_normalized = _portal_service_request(payload)
    try:
        result = get_interest_rx_matrix(service_payload)
    except InterestRxMatrixInputError as exc:
        _raise_brand_set_context_error(exc)
        raise HTTPException(status_code=400, detail={"error": "invalid_interest_rx_matrix_request", "message": str(exc)}) from exc
    if result is None:
        _raise_market_not_found(payload)
    return _success_response(result, request_normalized=request_normalized)


@router.post(
    "/jw-brand-activity-mock/api/brand-activity/interest-timeseries",
    include_in_schema=False,
)
@router.post(
    "/api/brand-activity/interest-timeseries",
    tags=[BRAND_ACTIVITY_TAG],
    summary="INTEREST 3구분 3년 월간 시계열",
    description=(
        "IQVIA CSD keyword INTEREST 3구분(VERY/SOMEWHAT/NOT USEFUL)의 브랜드별 월간 시계열입니다. "
        "요청에 기간 파라미터는 없으며, 데이터 최신월 기준 3년 전체를 항상 반환합니다(프론트가 절단). "
        "브랜드별·시점별 3구분 count와 브랜드 내 분모(total_count) 기준 pct를 제공하고, 데이터 없는 시점은 null입니다.\n\n"
        + BRAND_ACTIVITY_FILTER_DESCRIPTION
    ),
    response_model=None,
    openapi_extra={
        "requestBody": brand_activity_request_body(
            {
                "visit_location": {"type": ["string", "array"], "items": {"type": "string"}, "description": "키워드 종별 필터(HOSPITAL/PRIV. PRACTICE). 미전송/전체=전체."},
                "specialty": {"type": ["string", "array"], "items": {"type": "string"}, "description": "키워드 진료과 필터(19종). 미전송/전체=전체."},
            },
            {
                "view": "general",
                "selected_brand": "리바로",
                "filters": {"atc4": ["C10A1"]},
                "visit_location": "전체",
                "specialty": "전체",
            },
        )
    },
)
def brand_activity_interest_timeseries(payload: BrandActivityInterestTimeseriesRequest) -> dict[str, JsonValue]:
    """Return per-brand INTEREST 3-category monthly time series over a fixed 3-year window."""

    service_payload = _service_payload(payload)
    try:
        result = get_interest_timeseries(service_payload)
    except InterestTimeseriesInputError as exc:
        _raise_brand_set_context_error(exc)
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": "invalid_interest_timeseries_request", "message": str(exc)},
        ) from exc
    if result is None:
        _raise_market_not_found(payload)
    return _success_response(result, request_normalized=False)


def _portal_service_request(
    payload: CsdTimeseriesRequest | BrandActivityTopicsRequest | BrandActivityInterestRxRequest,
) -> tuple[dict[str, JsonValue], bool]:
    """Apply the nested BFF compatibility layer before the canonical flat contract."""

    filters_received = _received_filters(payload)
    flat_atc4 = filters_received.get("atc4")
    nested_atc = filters_received.get("atc")
    nested_atc4 = nested_atc.get("atc4") if isinstance(nested_atc, dict) else None
    has_flat_atc4 = flat_atc4 not in ({}, [], None)
    has_nested_atc4 = nested_atc4 not in ({}, [], None)
    if has_flat_atc4 and has_nested_atc4 and flat_atc4 != nested_atc4:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "conflicting_market_filter",
                "message": "filters.atc4 and filters.atc.atc4 must match when both are provided",
                "fields": ["filters.atc4", "filters.atc.atc4"],
            },
        )
    return _service_payload(payload), has_nested_atc4


def _success_response(
    result: dict[str, JsonValue],
    *,
    request_normalized: bool,
    metadata: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    response: dict[str, JsonValue] = {"data": result}
    meta = dict(metadata or {})
    if request_normalized:
        meta["request_normalized"] = True
    if meta:
        response["meta"] = meta
    return response


def _topic_period_metadata(payload: BrandActivityTopicsRequest, bounds: dict[str, str]) -> dict[str, JsonValue]:
    available_start = bounds.get("available_start") or ""
    available_end = bounds.get("available_end") or ""
    return {
        "start_date": payload.start_date or available_start,
        "end_date": payload.end_date or available_end,
        "available_start": available_start,
        "available_end": available_end,
    }


def _period_filter_active(payload: BrandActivityTopicsRequest) -> bool:
    return payload.start_date is not None or payload.end_date is not None


def _topic_result_has_data(result: dict[str, JsonValue]) -> bool:
    brands = result.get("brands")
    if not isinstance(brands, list):
        return False
    return any(isinstance(brand, dict) and int(brand.get("event_count") or 0) > 0 for brand in brands)


def _csd_unavailable_response(
    reason: str,
    message: str,
    *,
    csd_source_present: bool,
    candidates: list[dict[str, JsonValue]] | None = None,
) -> dict[str, JsonValue]:
    return {
        "data": {
            "available": False,
            "reason": reason,
            "message": message,
            "csd_source_present": csd_source_present,
            "candidates": candidates or [],
        }
    }


def _raise_brand_set_context_error(error: Exception) -> None:
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, BrandSetInputError) and cause.error != "invalid_brand_activity_request":
            raise HTTPException(status_code=cause.status_code, detail=cause.detail()) from error
        cause = cause.__cause__


def _raise_market_not_found(
    payload: CsdTimeseriesRequest | BrandActivityTopicsRequest | BrandActivityInterestRxRequest,
) -> Never:
    raise HTTPException(
        status_code=404,
        detail={
            "error": "market_not_found",
            "message": "요청 필터로 시장을 식별할 수 없음",
            "requested": {
                "view": payload.view,
                "filters_received": _received_filters(payload),
            },
            "hint": "flat filters.atc4 or market_id expected",
        },
    )


def _received_filters(
    payload: CsdTimeseriesRequest | CsdActivitySeriesRequest | BrandActivityTopicsRequest | BrandActivityInterestRxRequest,
) -> dict[str, JsonValue]:
    data = payload.model_dump()
    filters = _compact_filter(data.get("filters")) if isinstance(data.get("filters"), dict) else {}
    legacy_filter = _compact_filter(data.get("filter")) if isinstance(data.get("filter"), dict) else {}
    return filters or legacy_filter


def _service_payload(payload: CsdTimeseriesRequest | CsdActivitySeriesRequest | BrandActivityTopicsRequest | BrandActivityInterestRxRequest) -> dict[str, JsonValue]:
    """Normalize mock v0.1.7 `filters` while preserving legacy `filter` input."""

    data = payload.model_dump()
    if data.get("view") == "general":
        data.pop("market_id", None)
    normalized = _normalize_market_filter(_received_filters(payload))
    data["filters"] = normalized
    data["filter"] = normalized
    return data


def _normalize_market_filter(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    normalized = {key: item for key, item in value.items() if key not in {"atc", "channel"}}
    atc = value.get("atc")
    if isinstance(atc, dict):
        for key in ("atc4",):
            if key not in normalized and atc.get(key) not in ({}, [], None):
                normalized[key] = atc[key]

    atc4 = normalized.get("atc4")
    if isinstance(atc4, list):
        # ATC filters are a set; stable ordering keeps every Brand Activity route order-independent.
        normalized["atc4"] = sorted(canonical_atc4_values(atc4))

    audit_codes = _analysis_level_audit_codes(value)
    if not audit_codes:
        audit_codes = _legacy_channel_audit_codes(value)
    if audit_codes and "channel_axis" not in normalized:
        normalized["channel_axis"] = {"iqvia": {"audit_code": audit_codes}}
    return normalized


def _analysis_level_audit_codes(value: dict[str, JsonValue]) -> list[str]:
    analysis_level = value.get("analysis_level")
    if not isinstance(analysis_level, dict):
        return []
    iqvia = analysis_level.get("iqvia")
    if not isinstance(iqvia, dict):
        return []
    return _audit_code_list(iqvia.get("audit_code"))


def _legacy_channel_audit_codes(value: dict[str, JsonValue]) -> list[str]:
    channel = value.get("channel")
    if not isinstance(channel, dict):
        return []
    return _audit_code_list(channel.get("audit_code", channel.get("auditCode")))


def _audit_code_list(value: JsonValue) -> list[str]:
    raw_values = value if isinstance(value, list) else [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        code = str(item).strip().upper() if item is not None else ""
        if code and code not in seen:
            result.append(code)
            seen.add(code)
    return result


def _compact_filter(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    compacted: dict[str, JsonValue] = {}
    for key, item in value.items():
        compacted_item = _compact_value(item)
        if compacted_item not in ({}, [], None):
            compacted[key] = compacted_item
    return compacted


def _compact_value(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return _compact_filter(value)
    if isinstance(value, list):
        return [item for item in (_compact_value(item) for item in value) if item not in ({}, [], None)]
    return value
