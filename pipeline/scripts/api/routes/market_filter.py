"""Market filter helper routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from pipeline.scripts.api.market_filter_atc_options import build_market_filter_atc_options
from pipeline.scripts.api.dynamic_market.types import DynamicMarketInputError
from pipeline.scripts.api.models.market_filter import MarketFilterAtcOptionsRequest, MarketFilterAtcOptionsResponse


router = APIRouter(tags=["market-filter"])


@router.post(
    "/api/market-filter/atc-options",
    response_model=MarketFilterAtcOptionsResponse,
    summary="시장필터 1단계 ATC 옵션",
    description=(
        "브랜드, 뷰, 소스를 입력받아 ATC1/2/3/4 옵션 리스트를 반환합니다. "
        "선택 브랜드가 속한 ATC 노드는 flag=true로 표시되어 포탈 초기 체크/하이라이트에 사용할 수 있습니다."
    ),
)
def market_filter_atc_options(payload: MarketFilterAtcOptionsRequest) -> dict[str, object]:
    try:
        return build_market_filter_atc_options(
            brand_name=payload.brand_name,
            view=payload.view,
            source=payload.source,
        )
    except DynamicMarketInputError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_market_filter_atc_options_request", "message": str(exc)}) from exc


@router.get(
    "/api/market-filter/atc-options",
    response_model=MarketFilterAtcOptionsResponse,
    summary="시장필터 1단계 ATC 옵션(GET)",
    description="POST와 동일한 응답을 query parameter로 조회합니다. Swagger/포탈 연동 편의를 위한 읽기 전용 엔드포인트입니다.",
)
def market_filter_atc_options_get(
    brand_name: str = Query(..., description="[입력] 선택 브랜드명", examples=["리바로"]),
    view: str = Query("strategic", description="[입력] general 또는 strategic", examples=["strategic"]),
    source: str = Query("ubist", description="[입력] ubist, iqvia, iqvia_nsa", examples=["ubist"]),
) -> dict[str, object]:
    try:
        return build_market_filter_atc_options(brand_name=brand_name, view=view, source=source)
    except DynamicMarketInputError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_market_filter_atc_options_request", "message": str(exc)}) from exc
