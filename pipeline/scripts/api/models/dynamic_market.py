"""Request schemas for the dynamic market route."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DynamicMarketFilters(BaseModel):
    """Filter set accepted by the dynamic-market resolver."""

    model_config = ConfigDict(extra="forbid")

    atc4: list[str] = Field(default_factory=list, description="ATC4 codes included as an OR set.")
    molecule: list[str] = Field(default_factory=list, description="Molecules included as an OR set via bridge.")
    focus_brand_key: str | None = Field(default=None, description="Selected brand display name or normalized key.")
    view_kind: str | None = Field(default=None, description="Portal view kind such as market_landscape.")
    ml_id: str | None = Field(default=None, description="Strategic market-landscape market id.")
    cd_market_id: str | None = Field(default=None, description="Competitive-dynamics market id.")
    analysis_level: "DynamicMarketAnalysisLevelFilters" = Field(
        default_factory=lambda: DynamicMarketAnalysisLevelFilters()
    )


class DynamicMarketUbistLevelFilters(BaseModel):
    """UBIST analysis-level narrowing values."""

    model_config = ConfigDict(extra="forbid")

    seller: list[str] = Field(default_factory=list)
    molecule: list[str] = Field(default_factory=list)
    molecule_strength: list[str] = Field(default_factory=list)
    form: list[str] = Field(default_factory=list)
    route: list[str] = Field(default_factory=list)
    reimbursement: list[str] = Field(default_factory=list)
    atc3: list[str] = Field(default_factory=list)
    atc4: list[str] = Field(default_factory=list)


class DynamicMarketIqviaLevelFilters(BaseModel):
    """IQVIA analysis-level narrowing values."""

    model_config = ConfigDict(extra="forbid")

    mfr_name_kor: list[str] = Field(default_factory=list)
    molecule_type: list[str] = Field(default_factory=list)
    molecule_desc: list[str] = Field(default_factory=list)
    pack_desc: list[str] = Field(default_factory=list)
    strength: list[str] = Field(default_factory=list)
    nhi_type: list[str] = Field(default_factory=list)
    audit_code: list[str] = Field(default_factory=list)


class DynamicMarketAnalysisLevelFilters(BaseModel):
    """Source-specific strategic analysis-level narrowing filters."""

    model_config = ConfigDict(extra="forbid")

    ubist: DynamicMarketUbistLevelFilters = Field(default_factory=DynamicMarketUbistLevelFilters)
    iqvia: DynamicMarketIqviaLevelFilters = Field(default_factory=DynamicMarketIqviaLevelFilters)


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
