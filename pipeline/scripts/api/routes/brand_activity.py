from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pipeline.scripts.api.brand_activity_csd_timeseries import (
    CsdTimeseriesAmbiguousMarketError,
    CsdTimeseriesInputError,
    get_csd_timeseries,
)
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
from pipeline.scripts.api.brand_activity_topic_matrix import (
    TopicRequestError,
    get_topic_brand_payload,
)
from pipeline.scripts.api.brand_activity_topics import (
    JsonValue,
    TopicPayloadError,
    get_topic_payload,
    list_topic_payloads,
)
from pipeline.scripts.api.models.brand_activity import (
    BrandActivityInterestRxRequest,
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
)


router = APIRouter()


# Internal diagnostic/storage endpoints. The portal-facing Brand Activity contract is the POST matrix route below.
TOPIC_DEBUG_INCLUDE_IN_SCHEMA = False


@router.get("/api/brand-activity/topics", include_in_schema=TOPIC_DEBUG_INCLUDE_IN_SCHEMA)
def brand_activity_topics() -> dict[str, JsonValue]:
    """Return all Brand Activity topic market payloads."""
    try:
        return {"data": list_topic_payloads()}
    except TopicPayloadError as exc:
        raise HTTPException(status_code=500, detail={"error": "invalid_brand_activity_topic_payload"}) from exc


@router.get("/api/brand-activity/topics/{scope_id}", include_in_schema=TOPIC_DEBUG_INCLUDE_IN_SCHEMA)
def brand_activity_topic(scope_id: str) -> dict[str, JsonValue]:
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
        "topic_shares 합 + etc_pct = 100이며, event_count=0이면 topic_shares는 빈 배열입니다.\n\n"
        + BRAND_ACTIVITY_FILTER_DESCRIPTION
    ),
    response_model=None,
    openapi_extra={"requestBody": {"content": {"application/json": {"example": BRAND_ACTIVITY_TOPICS_REQUEST_EXAMPLE}}}},
    responses=BRAND_ACTIVITY_TOPICS_RESPONSES,
)
def brand_activity_topic_matrix(payload: BrandActivityTopicsRequest) -> dict[str, JsonValue]:
    """Return selected and competitor brand topic shares."""

    try:
        result = get_topic_brand_payload(_service_payload(payload))
    except TopicRequestError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_brand_activity_topic_request", "message": str(exc)}) from exc
    except TopicPayloadError as exc:
        raise HTTPException(status_code=500, detail={"error": "invalid_brand_activity_topic_payload"}) from exc
    if result is None:
        return {"data": None, "reason": "market_not_found"}
    return {"data": result}


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
        "IQVIA 처방 지표(unit/counting_unit/dosage_unit)는 같은 분기축으로 정렬됩니다.\n\n"
        + BRAND_ACTIVITY_FILTER_DESCRIPTION
    ),
    response_model=None,
    openapi_extra={"requestBody": {"content": {"application/json": {"example": BRAND_ACTIVITY_CSD_TIMESERIES_REQUEST_EXAMPLE}}}},
    responses=BRAND_ACTIVITY_CSD_TIMESERIES_RESPONSES,
)
def brand_activity_csd_timeseries(payload: CsdTimeseriesRequest) -> dict[str, JsonValue]:
    """Return integrated CSD activity and IQVIA prescription timeseries."""

    try:
        result = get_csd_timeseries(_service_payload(payload))
    except CsdTimeseriesInputError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_csd_timeseries_request", "message": str(exc)}) from exc
    except CsdTimeseriesAmbiguousMarketError as exc:
        return {"data": None, "reason": "csd_market_ambiguous", "message": str(exc)}
    if result is None:
        return {"data": None, "reason": "market_not_found"}
    return {"data": result}


@router.post(
    "/api/brand-activity/csd-activity-series",
    tags=[BRAND_ACTIVITY_TAG],
    summary="CSD 활동량·비율·순위 추세",
    description=(
        "문서 Section 1 CSD Channeldynamics 시계열 API입니다. "
        "기존 /csd-timeseries와 별도로 CSD jw_channel 선택, 회사축, 활동량 rank series를 제공합니다."
    ),
    response_model=None,
    openapi_extra={"requestBody": {"content": {"application/json": {"example": CSD_ACTIVITY_SERIES_EXAMPLE}}}},
)
def brand_activity_csd_activity_series(payload: CsdActivitySeriesRequest) -> dict[str, JsonValue]:
    """Return Section 1 CSD activity volume, share, and rank time series."""

    try:
        result = get_csd_activity_series(_service_payload(payload))
    except CsdActivitySeriesInputError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_csd_activity_series_request", "message": str(exc)}) from exc
    except CsdTimeseriesAmbiguousMarketError as exc:
        return {"data": None, "reason": "csd_market_ambiguous", "message": str(exc)}
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
        "X축은 rx_frequency_score, Y축은 interest_score, 버블 면적은 event_count입니다. "
        "market_average는 화면의 점선 십자 기준선입니다.\n\n"
        + BRAND_ACTIVITY_FILTER_DESCRIPTION
    ),
    response_model=None,
    openapi_extra={"requestBody": {"content": {"application/json": {"example": BRAND_ACTIVITY_INTEREST_RX_REQUEST_EXAMPLE}}}},
    responses=BRAND_ACTIVITY_INTEREST_RX_RESPONSES,
)
def brand_activity_interest_rx_matrix(payload: BrandActivityInterestRxRequest) -> dict[str, JsonValue]:
    """Return interest/Rx distributions and detailing for selected brands."""

    try:
        result = get_interest_rx_matrix(_service_payload(payload))
    except InterestRxMatrixInputError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_interest_rx_matrix_request", "message": str(exc)}) from exc
    if result is None:
        return {"data": None, "reason": "market_not_found"}
    return {"data": result}


def _service_payload(payload: CsdTimeseriesRequest | CsdActivitySeriesRequest | BrandActivityTopicsRequest | BrandActivityInterestRxRequest) -> dict[str, JsonValue]:
    """Normalize mock v0.1.7 `filters` while preserving legacy `filter` input."""

    data = payload.model_dump()
    filters = _compact_filter(data.get("filters")) if isinstance(data.get("filters"), dict) else {}
    legacy_filter = _compact_filter(data.get("filter")) if isinstance(data.get("filter"), dict) else {}
    normalized = _normalize_market_filter(filters or legacy_filter)
    data["filters"] = normalized
    data["filter"] = normalized
    # The service layer resolves market scope from selected_brand + filters.
    # Keep market_id documented as compatibility input, but do not change the
    # historical service payload contract for existing Brand Activity handlers.
    data.pop("market_id", None)
    return data


def _normalize_market_filter(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    normalized = {key: item for key, item in value.items() if key not in {"atc", "channel"}}
    atc = value.get("atc")
    if isinstance(atc, dict):
        for key in ("atc3", "atc4"):
            if key not in normalized and atc.get(key) not in ({}, [], None):
                normalized[key] = atc[key]

    channel = value.get("channel")
    if isinstance(channel, dict):
        for key in ("visit_location", "specialty"):
            if key not in normalized and channel.get(key) not in ({}, [], None):
                normalized[key] = channel[key]

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
