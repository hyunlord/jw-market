"""Market filter helper routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from pipeline.scripts.api.market_filter_atc_options import build_market_filter_atc_options
from pipeline.scripts.api.dynamic_market.types import DynamicMarketInputError
from pipeline.scripts.api.models.market_filter import MarketFilterAtcOptionsResponse, MarketFilterSource, MarketFilterView
from pipeline.scripts.api.openapi_docs import ATC_OPTIONS_RESPONSES, DYNAMIC_MARKET_TAG


router = APIRouter()


@router.get(
    "/api/market-filter/atc-options",
    response_model=MarketFilterAtcOptionsResponse,
    tags=[DYNAMIC_MARKET_TAG],
    summary="시장필터 1단계 ATC 옵션",
    description=(
        "조회 전용 GET 엔드포인트입니다. 브랜드, 뷰, 공개 소스(ubist/iqvia)를 입력받아 "
        "ATC1/2/3/4 옵션을 key/level/parent/flag 형태로 반환합니다. "
        "flag=true는 선택 브랜드가 해당 ATC 노드에 속한다는 뜻이며, 프론트에서는 초기 선택/locked 표시 기준으로 사용합니다."
    ),
    responses=ATC_OPTIONS_RESPONSES,
)
def market_filter_atc_options_get(
    brand_name: str | None = Query(
        None,
        description="[입력] 선택 브랜드명. general에서는 생략할 수 있으며, 생략 시 전체 ATC universe를 반환합니다.",
        examples=["리바로"],
    ),
    view: MarketFilterView = Query("strategic", description="[입력] general 또는 strategic", examples=["strategic"]),
    source: MarketFilterSource = Query("ubist", description="[입력] ubist 또는 iqvia. 내부 iqvia_nsa 값은 노출하지 않습니다.", examples=["ubist"]),
) -> dict[str, object]:
    try:
        return build_market_filter_atc_options(brand_name=brand_name, view=view, source=source)
    except DynamicMarketInputError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_market_filter_atc_options_request", "message": str(exc)}) from exc
