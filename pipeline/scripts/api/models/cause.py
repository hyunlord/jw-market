from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ExtendedMetricBlock(BaseModel):
    metric_basis: Literal["canonical_value"] = "canonical_value"
    cagr_1y: float | None = None
    cagr_3y: float | None = None
    cagr_5y: float | None = None
    ei_5y: float | None = None
    momentum_score: float | None = None
    growth_contribution: float | None = None


class MarketContext(BaseModel):
    hhi: float | None = None
    market_cagr_5y: float | None = None


class CauseSummary(BaseModel):
    market_share: float | None = None
    rank_in_market: int | None = None
    mom: float | None = None
    qoq: float | None = None
    yoy: float | None = None
    mat: float | None = None
    growth_abs: float | None = None
    extended: ExtendedMetricBlock = Field(default_factory=ExtendedMetricBlock)
    market_context: MarketContext = Field(default_factory=MarketContext)


class CausePoint(BaseModel):
    period_yyyymm: str
    market_share: float | None = None
    mom: float | None = None
    qoq: float | None = None
    yoy: float | None = None
    mat: float | None = None
    growth_abs: float | None = None
    rank_in_market: int | None = None
    extended: ExtendedMetricBlock | None = None
    market_context: MarketContext | None = None
    warnings: list[str] = Field(default_factory=list)


class CauseDriver(BaseModel):
    type: str
    metric: str
    value: float | None = None
    severity: Literal["info", "warning", "critical"] = "info"
    explanation: str


class CauseResponse(BaseModel):
    brand: str
    resolved_brand_id: str
    resolved_brand_name: str
    market_id: str
    view: str
    source: str
    measure: str
    unit_label: str
    period_yyyymm: str
    summary: CauseSummary | None = None
    monthly: list[CausePoint] = Field(default_factory=list)
    drivers: list[CauseDriver] = Field(default_factory=list)
    market_context: MarketContext = Field(default_factory=MarketContext)
    data: dict | None = None
    reason: str | None = None
    generated_at: str
