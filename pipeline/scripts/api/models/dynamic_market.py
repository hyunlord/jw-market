"""Request schemas for the dynamic market route."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DynamicMarketFilters(BaseModel):
    """Filter set accepted by the general-view MVP resolver."""

    model_config = ConfigDict(extra="forbid")

    atc4: list[str] = Field(default_factory=list, description="ATC4 codes included as an OR set.")
    molecule: list[str] = Field(default_factory=list, description="Molecules included as an OR set via bridge.")


class DynamicMarketPeriodRange(BaseModel):
    """Inclusive period range in ``YYYY-MM`` format."""

    model_config = ConfigDict(extra="forbid")

    start: str | None = None
    end: str | None = None


class DynamicMarketOptions(BaseModel):
    """Runtime knobs that do not change market identity."""

    model_config = ConfigDict(extra="forbid")

    top_n: int | None = Field(default=20, ge=1, le=100)
    metrics: list[str] = Field(default_factory=list)
    period_range: DynamicMarketPeriodRange | None = None


class DynamicMarketRequest(BaseModel):
    """Request body for ``POST /api/dynamic-market``."""

    model_config = ConfigDict(extra="forbid")

    filters: DynamicMarketFilters = Field(default_factory=DynamicMarketFilters)
    source: str = "ubist"
    measure: str = "sales"
    options: DynamicMarketOptions = Field(default_factory=DynamicMarketOptions)
