from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pipeline.scripts.api.brand_activity_csd_timeseries import (
    CsdTimeseriesAmbiguousMarketError,
    CsdTimeseriesInputError,
    get_csd_timeseries,
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


router = APIRouter()


@router.get("/api/brand-activity/topics", include_in_schema=False)
def brand_activity_topics() -> dict[str, JsonValue]:
    """Return all Brand Activity topic market payloads."""
    try:
        return {"data": list_topic_payloads()}
    except TopicPayloadError as exc:
        raise HTTPException(status_code=500, detail={"error": "invalid_brand_activity_topic_payload"}) from exc


@router.get("/api/brand-activity/topics/{scope_id}", include_in_schema=False)
def brand_activity_topic(scope_id: str) -> dict[str, JsonValue]:
    """Return one Brand Activity topic market payload."""
    try:
        payload = get_topic_payload(scope_id)
    except TopicPayloadError as exc:
        raise HTTPException(status_code=500, detail={"error": "invalid_brand_activity_topic_payload"}) from exc
    if payload is None:
        return {"data": None, "reason": "scope_not_found", "scope_id": scope_id}
    return {"data": payload}


@router.post("/api/brand-activity/topics")
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


@router.post("/api/brand-activity/csd-timeseries")
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


@router.post("/api/brand-activity/interest-rx-matrix")
def brand_activity_interest_rx_matrix(payload: BrandActivityInterestRxRequest) -> dict[str, JsonValue]:
    """Return interest/Rx distributions and detailing for selected brands."""

    try:
        result = get_interest_rx_matrix(_service_payload(payload))
    except InterestRxMatrixInputError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_interest_rx_matrix_request", "message": str(exc)}) from exc
    if result is None:
        return {"data": None, "reason": "market_not_found"}
    return {"data": result}


def _service_payload(payload: CsdTimeseriesRequest | BrandActivityTopicsRequest | BrandActivityInterestRxRequest) -> dict[str, JsonValue]:
    """Normalize mock v0.1.7 `filters` while preserving legacy `filter` input."""

    data = payload.model_dump()
    filters = _compact_filter(data.get("filters")) if isinstance(data.get("filters"), dict) else {}
    legacy_filter = _compact_filter(data.get("filter")) if isinstance(data.get("filter"), dict) else {}
    normalized = filters or legacy_filter
    data["filters"] = normalized
    data["filter"] = normalized
    return data


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
