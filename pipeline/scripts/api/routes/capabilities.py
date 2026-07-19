from __future__ import annotations

from fastapi import APIRouter

from pipeline.scripts.api.capabilities_registry import build_capabilities
from pipeline.scripts.api.openapi_docs import META_TAG


router = APIRouter()

# 정적 계약이므로 프로세스 캐시로 한 번만 구성한다.
_CAPABILITIES = build_capabilities()


@router.get(
    "/api/capabilities",
    tags=[META_TAG],
    summary="기계 판독용 지표/뷰 계약",
    description=(
        "E-2 소비자가 지표 스위치를 하드코딩하지 않도록, 코드에서 생성한 계약을 반환합니다. "
        "지표군(cause/dynamic-market/deep-analysis)·view enum(general/strategic_ml/strategic_cd)·"
        "기간 앵커·deprecated 필드·market_id 존치 여부를 포함합니다."
    ),
    response_model=None,
)
def capabilities() -> dict:
    return _CAPABILITIES
