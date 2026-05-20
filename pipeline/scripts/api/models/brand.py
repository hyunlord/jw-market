from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BrandResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand: str
    market_id: str
    market_name: str
    market_name_short: str
    market_label_kor: str
    mkt_team: str | None
    sources: list[str]
    atc_codes: list[str]
    atc_desc: str
    is_jw: bool
    is_target: bool
    is_dual_source: bool
    rank: int


class BrandInfo(BaseModel):
    brand: str
    market_id: str
    ml_id: str
    market_name: str | None = None
    source_class: str
    sources: list[str]
    available_measures: dict[str, list[str]] = Field(default_factory=dict)
    cause_variants: int
    resolved_brand_id: str | None = None
    resolved_brand_name: str | None = None
    snapshot: dict[str, Any] | None = None


class BrandsResponse(BaseModel):
    data: list[BrandInfo]
    total: int
    generated_at: str
