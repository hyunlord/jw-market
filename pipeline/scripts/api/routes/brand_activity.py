from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

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


router = APIRouter()


class CsdTimeseriesWindow(BaseModel):
    """Optional inclusive quarter window for Brand Activity CSD timeseries."""

    model_config = ConfigDict(extra="forbid")

    start: str | None = None
    end: str | None = None


class CsdTimeseriesRequest(BaseModel):
    """Request body for the Brand Activity integrated CSD timeseries route."""

    model_config = ConfigDict(extra="forbid")

    view: str
    market_id: str
    selected_brand: str
    filter: dict[str, JsonValue] = Field(default_factory=dict)
    window: CsdTimeseriesWindow | None = None


class BrandActivityTopicsRequest(BaseModel):
    """Request body for the filtered Brand Activity topic route."""

    model_config = ConfigDict(extra="forbid")

    view: str
    market_id: str
    selected_brand: str
    filter: dict[str, JsonValue] = Field(default_factory=dict)
    visit_location: str = "전체"
    specialty: str = "전체"
    top_n: int = Field(default=5, ge=1, le=5)


class InterestRxWeights(BaseModel):
    """Optional score-weight overrides for interest/Rx matrix axes."""

    model_config = ConfigDict(extra="forbid")

    interest: dict[str, float] = Field(default_factory=dict)
    rx_frequency: dict[str, float] = Field(default_factory=dict)
    prescription_evolution: dict[str, float] = Field(default_factory=dict)


class BrandActivityInterestRxRequest(BaseModel):
    """Request body for the Brand Activity interest/Rx matrix route."""

    model_config = ConfigDict(extra="forbid")

    view: str
    market_id: str
    selected_brand: str
    filter: dict[str, JsonValue] = Field(default_factory=dict)
    visit_location: str = "전체"
    specialty: str = "전체"
    period_start: str | None = None
    period_end: str | None = None
    weights: InterestRxWeights | None = None


@router.get("/api/brand-activity/topics")
def brand_activity_topics() -> dict[str, JsonValue]:
    """Return all Brand Activity topic market payloads."""
    try:
        return {"data": list_topic_payloads()}
    except TopicPayloadError as exc:
        raise HTTPException(status_code=500, detail={"error": "invalid_brand_activity_topic_payload"}) from exc


@router.get("/api/brand-activity/topics/{scope_id}")
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
        result = get_topic_brand_payload(payload.model_dump())
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
        result = get_csd_timeseries(payload.model_dump())
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
        result = get_interest_rx_matrix(payload.model_dump())
    except InterestRxMatrixInputError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_interest_rx_matrix_request", "message": str(exc)}) from exc
    if result is None:
        return {"data": None, "reason": "market_not_found"}
    return {"data": result}
