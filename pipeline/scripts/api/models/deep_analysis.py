from __future__ import annotations

from pydantic import BaseModel, Field


class DeepAnalysisChannelSpec(BaseModel):
    channel: str
    specialty: str
    market_share: float | None = None
    rank: int | None = None
    cagr_5y: float | None = None
    ei_5y: float | None = None
    momentum_score: float | None = None
    growth_contribution: float | None = None
    hhi: float | None = None
    market_cagr_5y: float | None = None


class DeepAnalysisResponse(BaseModel):
    brand: str
    resolved_brand_id: str
    resolved_brand_name: str
    market_id: str
    period_yyyymm: str
    breakdown: list[DeepAnalysisChannelSpec] = Field(default_factory=list)
    data: dict = Field(default_factory=dict)
    generated_at: str
