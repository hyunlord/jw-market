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
from pipeline.scripts.api.openapi_docs import (
    BRAND_ACTIVITY_CSD_TIMESERIES_REQUEST_EXAMPLE,
    BRAND_ACTIVITY_CSD_TIMESERIES_RESPONSES,
    BRAND_ACTIVITY_INTEREST_RX_REQUEST_EXAMPLE,
    BRAND_ACTIVITY_INTEREST_RX_RESPONSES,
    BRAND_ACTIVITY_TAG,
    BRAND_ACTIVITY_TOPICS_REQUEST_EXAMPLE,
    BRAND_ACTIVITY_TOPICS_RESPONSES,
)


router = APIRouter()


class CsdTimeseriesWindow(BaseModel):
    """Optional inclusive quarter window for Brand Activity CSD timeseries."""

    model_config = ConfigDict(extra="ignore")

    start: str | None = Field(default=None, description="포함 시작 분기. 예: 2024Q1")
    end: str | None = Field(default=None, description="포함 종료 분기. 예: 2025Q4")


class CsdTimeseriesRequest(BaseModel):
    """Request body for the Brand Activity integrated CSD timeseries route."""

    model_config = ConfigDict(extra="ignore")

    view: str = Field(description="분석 뷰. general 또는 strategic_ml.")
    market_id: str | None = Field(default=None, description="일반뷰 ATC4 또는 전략 ml_id. general은 filters.atc4 첫 값으로 대체 가능.")
    selected_brand: str = Field(description="강조/시장 결정 브랜드.")
    filters: dict[str, JsonValue] = Field(default_factory=dict, description="시장·차원 필터. 신규 계약 필드.")
    filter: dict[str, JsonValue] = Field(default_factory=dict, description="legacy 호환 필드. filters가 있으면 filters가 우선.")
    mode: str = Field(default="absolute", description="absolute 또는 share. 화면에서 series.absolute/ratio 선택에 사용.")
    window: CsdTimeseriesWindow | None = Field(default=None, description="분기 window. 미지정 시 CSD full quarter 범위.")


class BrandActivityTopicsRequest(BaseModel):
    """Request body for the filtered Brand Activity topic route."""

    model_config = ConfigDict(extra="ignore")

    view: str = Field(description="분석 뷰. general 또는 strategic_ml.")
    market_id: str | None = Field(default=None, description="일반뷰 ATC4 또는 전략 ml_id. general은 filters.atc4 첫 값으로 대체 가능.")
    selected_brand: str = Field(description="강조/시장 결정 브랜드.")
    filters: dict[str, JsonValue] = Field(default_factory=dict, description="시장·차원 필터. 신규 계약 필드.")
    filter: dict[str, JsonValue] = Field(default_factory=dict, description="legacy 호환 필드. filters가 있으면 filters가 우선.")
    visit_location: str = Field(default="전체", description="키워드 설문 방문 장소 필터.")
    specialty: str = Field(default="전체", description="키워드 설문 진료과 필터.")
    top_n: int = Field(default=5, ge=1, le=10, description="브랜드 카드당 상위 토픽 개수.")


class InterestRxWeights(BaseModel):
    """Optional score-weight overrides for interest/Rx matrix axes."""

    model_config = ConfigDict(extra="ignore")

    interest: dict[str, float] = Field(default_factory=dict, description="interest 레벨별 score 가중치 override.")
    rx_frequency: dict[str, float] = Field(default_factory=dict, description="처방빈도 레벨별 score 가중치 override.")
    prescription_evolution: dict[str, float] = Field(default_factory=dict, description="처방 변화 레벨별 score 가중치 override.")


class BrandActivityInterestRxRequest(BaseModel):
    """Request body for the Brand Activity interest/Rx matrix route."""

    model_config = ConfigDict(extra="ignore")

    view: str = Field(description="분석 뷰. general 또는 strategic_ml.")
    market_id: str | None = Field(default=None, description="일반뷰 ATC4 또는 전략 ml_id. general은 filters.atc4 첫 값으로 대체 가능.")
    selected_brand: str = Field(description="강조/시장 결정 브랜드.")
    filters: dict[str, JsonValue] = Field(default_factory=dict, description="시장·차원 필터. 신규 계약 필드.")
    filter: dict[str, JsonValue] = Field(default_factory=dict, description="legacy 호환 필드. filters가 있으면 filters가 우선.")
    visit_location: str = Field(default="전체", description="키워드 설문 방문 장소 필터.")
    specialty: str = Field(default="전체", description="키워드 설문 진료과 필터.")
    period_start: str | None = Field(default=None, description="집계 시작월 YYYY-MM.")
    period_end: str | None = Field(default=None, description="집계 종료월 YYYY-MM.")
    weights: InterestRxWeights | None = Field(default=None, description="score 계산 가중치 override. 미지정 시 서버 기본값.")


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


@router.post(
    "/api/brand-activity/topics",
    tags=[BRAND_ACTIVITY_TAG],
    summary="브랜드별 토픽 그리드",
    description=(
        "mock `/jw-brand-activity-mock/api/brand-activity/topics`와 대응되는 포탈 공유 API입니다. "
        "브랜드 카드별 event_count, topic_shares, etc_pct, brand_specific_topics를 반환합니다. "
        "topic_shares 합 + etc_pct = 100이며, event_count=0이면 topic_shares는 빈 배열입니다."
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
    "/api/brand-activity/csd-timeseries",
    tags=[BRAND_ACTIVITY_TAG],
    summary="활동·처방 추세",
    description=(
        "mock `/jw-brand-activity-mock/api/brand-activity/csd-timeseries`와 대응되는 포탈 공유 API입니다. "
        "CSD 활동량은 `csd_channel_dynamics_stage`의 `jw_channel='TOTAL'`만 사용하므로 화면 관점의 region=TOTAL입니다. "
        "IQVIA 처방 지표(unit/counting_unit/dosage_unit)는 같은 분기축으로 정렬됩니다."
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
    "/api/brand-activity/interest-rx-matrix",
    tags=[BRAND_ACTIVITY_TAG],
    summary="interest×처방빈도 버블",
    description=(
        "mock `/jw-brand-activity-mock/api/brand-activity/interest-rx-matrix`와 대응되는 포탈 공유 API입니다. "
        "X축은 rx_frequency_score, Y축은 interest_score, 버블 면적은 event_count입니다. "
        "market_average는 화면의 점선 십자 기준선입니다."
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


def _service_payload(payload: BaseModel) -> dict[str, JsonValue]:
    """Normalize mock v0.1.7 `filters` while preserving legacy `filter` input."""

    data = payload.model_dump()
    filters = data.get("filters") if isinstance(data.get("filters"), dict) else {}
    legacy_filter = data.get("filter") if isinstance(data.get("filter"), dict) else {}
    normalized = filters or legacy_filter
    data["filters"] = normalized
    data["filter"] = normalized
    return data
