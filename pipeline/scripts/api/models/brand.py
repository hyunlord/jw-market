from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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
