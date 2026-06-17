"""Pydantic schemas for the dynamic market route.

The response describes a runtime-computed market, not a persisted cache row.
``market_definition`` explains which brands were included; ``metrics`` carries
the aggregate values over the requested period range.
"""

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


class DynamicMarketBrand(BaseModel):
    """One top-N brand contribution in the dynamic market response."""

    model_config = ConfigDict(extra="forbid")

    brand_key: str
    brand_name: str
    atc4_code: str
    total_value: float
    market_share_pct: float
    rank: int
    latest_period: str | None
    latest_value: float | None
    monthly_series: list[dict[str, float | str]]


class DynamicMarketDefinition(BaseModel):
    """Resolved runtime market definition echoed to the caller."""

    model_config = ConfigDict(extra="forbid")

    filter_echo: dict[str, list[str] | str | None]
    brand_count: int
    brand_list: list[dict[str, str]]


class DynamicMarketMetrics(BaseModel):
    """Aggregated runtime metrics shared by general and future strategic views."""

    model_config = ConfigDict(extra="forbid")

    market_size: float
    hhi: float | None
    cagr: float | None
    monthly_series: list[dict[str, float | str]]
    brands: list[DynamicMarketBrand]


class DynamicMarketResponse(BaseModel):
    """Public response for one dynamic market computation."""

    model_config = ConfigDict(extra="forbid")

    market_definition: DynamicMarketDefinition
    metrics: DynamicMarketMetrics
    computed: str = "runtime"
