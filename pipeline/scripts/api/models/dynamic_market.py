"""Request schemas for the dynamic market route."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class UbistAnalysisLevel(BaseModel):
    """UBIST product-level dynamic filters from the mock OpenAPI contract."""

    model_config = ConfigDict(extra="forbid")

    seller: list[str] = Field(default_factory=list)
    molecule: list[str] = Field(default_factory=list, description="Disabled by PL policy for D-1.")
    molecule_strength: list[str] = Field(default_factory=list)
    form: list[str] = Field(default_factory=list)
    route: list[str] = Field(default_factory=list)
    reimbursement: list[str] = Field(default_factory=list)


class IqviaAnalysisLevel(BaseModel):
    """IQVIA product-level dynamic filters from the mock OpenAPI contract."""

    model_config = ConfigDict(extra="forbid")

    mfr_name_kor: list[str] = Field(default_factory=list)
    molecule_type: list[str] = Field(default_factory=list)
    molecule_desc: list[str] = Field(default_factory=list, description="Disabled by PL policy for D-1.")
    pack_desc: list[str] = Field(default_factory=list, description="Disabled by PL policy for D-1.")
    strength: list[str] = Field(default_factory=list)
    nhi_type: list[str] = Field(default_factory=list)


class DynamicMarketAnalysisLevel(BaseModel):
    """Source-specific analysis-level filters.

    UBIST and IQVIA dimensions are deliberately not mapped together; the
    selected ``source`` determines which nested object is accepted.
    """

    model_config = ConfigDict(extra="forbid")

    ubist: UbistAnalysisLevel = Field(default_factory=UbistAnalysisLevel)
    iqvia: IqviaAnalysisLevel = Field(default_factory=IqviaAnalysisLevel)


class DynamicMarketFilters(BaseModel):
    """Filter set accepted by the general-view MVP resolver."""

    model_config = ConfigDict(extra="forbid")

    atc4: list[str] = Field(default_factory=list, description="ATC4 codes included as an OR set.")
    molecule: list[str] = Field(default_factory=list, description="Molecules included as an OR set via bridge.")
    focus_brand_key: str | None = Field(default=None, description="Selected brand kept visible when dimension filters narrow the market.")
    analysis_level: DynamicMarketAnalysisLevel = Field(default_factory=DynamicMarketAnalysisLevel)


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
