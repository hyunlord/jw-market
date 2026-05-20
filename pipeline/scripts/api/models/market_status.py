from __future__ import annotations

from pydantic import BaseModel


class MarketStatusBrand(BaseModel):
    brand_id: str
    brand_name: str
    is_jw: bool
    market_share: float | None = None
    rank: int | None = None
    cagr_5y: float | None = None
    ei_5y: float | None = None


class MarketStatusMl(BaseModel):
    ml_id: str
    ml_name: str | None = None
    period_yyyymm: str
    hhi: float | None = None
    market_cagr_5y: float | None = None
    top_brands: list[MarketStatusBrand]
    total_brands: int


class MarketStatusResponse(BaseModel):
    period: str
    markets: list[MarketStatusMl]
    total_markets: int
    generated_at: str
