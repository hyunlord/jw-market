"""Request schemas for the dynamic market route."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class UbistAnalysisLevel(BaseModel):
    """UBIST product-level dynamic filters from the mock OpenAPI contract."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    class_: list[str] = Field(default_factory=list, alias="class", description="UBIST Class 필터. 값 목록 안에서는 OR로 결합합니다.", examples=[["Statin"]])
    seller: list[str] = Field(default_factory=list, description="판매사 필터. 값 목록 안에서는 OR로 결합합니다.", examples=[["JW중외제약"]])
    molecule: list[str] = Field(default_factory=list, description="성분 필터. 값 목록 안에서는 OR로 결합합니다.", examples=[["PITAVASTATIN"]])
    molecule_strength: list[str] = Field(default_factory=list, description="성분용량 필터.", examples=[["PITAVASTATIN 2MG"]])
    strength_pack: list[str] = Field(default_factory=list, description="용량/포장 단위 필터.", examples=[["2mg"]])
    ox_gx: list[str] = Field(default_factory=list, description="오리지널/제네릭 구분 필터.", examples=[["Original"]])
    form: list[str] = Field(default_factory=list, description="제형 필터.", examples=[["정제"]])
    route: list[str] = Field(default_factory=list, description="투여경로 필터.", examples=[["경구"]])
    reimbursement: list[str] = Field(default_factory=list, description="급여구분 필터.", examples=[["급여"]])
    atc3: list[str] = Field(default_factory=list, description="ATC3 narrowing 필터.", examples=[["C10A"]])
    atc4: list[str] = Field(default_factory=list, description="ATC4 narrowing 필터.", examples=[["C10A1"]])


class IqviaAnalysisLevel(BaseModel):
    """IQVIA product-level dynamic filters from the mock OpenAPI contract."""

    model_config = ConfigDict(extra="forbid")

    mfr: list[str] = Field(default_factory=list, description="IQVIA 제조사 필터.", examples=[["JW중외제약"]])
    mfr_name_kor: list[str] = Field(default_factory=list, description="IQVIA 제조사 한글명 필터.", examples=[["JW중외제약"]])
    molecule_type: list[str] = Field(default_factory=list, description="IQVIA molecule type 필터.")
    molecule_desc: list[str] = Field(default_factory=list, description="IQVIA molecule desc 성분 필터.")
    pack_desc: list[str] = Field(default_factory=list, description="IQVIA pack desc 필터.")
    strength: list[str] = Field(default_factory=list, description="IQVIA strength 필터.")
    nhi: list[str] = Field(default_factory=list, description="IQVIA NHI 필터.")
    nhi_type: list[str] = Field(default_factory=list, description="IQVIA NHI type 필터.")
    audit_code: list[str] = Field(default_factory=list, description="IQVIA audit code 필터.")


class DynamicMarketAnalysisLevel(BaseModel):
    """Source-specific analysis-level filters.

    UBIST and IQVIA dimensions are deliberately not mapped together; the
    selected ``source`` determines which nested object is accepted.
    """

    model_config = ConfigDict(extra="forbid")

    ubist: UbistAnalysisLevel = Field(default_factory=UbistAnalysisLevel)
    iqvia: IqviaAnalysisLevel = Field(default_factory=IqviaAnalysisLevel)


DynamicMarketAnalysisLevelFilters = DynamicMarketAnalysisLevel


class DynamicMarketFilters(BaseModel):
    """Filter set accepted by general and strategic dynamic resolvers."""

    model_config = ConfigDict(extra="forbid")

    atc4: list[str] = Field(default_factory=list, description="일반뷰 ATC4 OR 범위. 최소 하나의 atc4 또는 molecule이 필요합니다.", examples=[["C10A1", "C10C0"]])
    molecule: list[str] = Field(default_factory=list, description="일반뷰 molecule OR 범위.", examples=[["PITAVASTATIN"]])
    view_kind: str | None = Field(default=None, description="전략뷰 종류. market_landscape 또는 competitive_dynamics.", examples=["market_landscape"])
    ml_id: str | None = Field(default=None, description="전략 market_landscape id.", examples=["ml_006"])
    cd_market_id: str | None = Field(default=None, description="전략 competitive_dynamics id.", examples=["cd_001"])
    focus_brand_key: str | None = Field(default=None, description="선택 브랜드명. narrowing 후에도 브랜드 자신을 유지할 때 사용합니다.", examples=["리바로"])
    analysis_level: DynamicMarketAnalysisLevel = Field(default_factory=DynamicMarketAnalysisLevel)


class DynamicMarketPeriodRange(BaseModel):
    """Inclusive period range in ``YYYY-MM`` format."""

    model_config = ConfigDict(extra="forbid")

    start: str | None = Field(default=None, description="시작 period YYYY-MM.", examples=["2025-01"])
    end: str | None = Field(default=None, description="종료 period YYYY-MM.", examples=["2026-04"])


class DynamicMarketOptions(BaseModel):
    """Runtime knobs that do not change market identity."""

    model_config = ConfigDict(extra="forbid")

    top_n: int | None = Field(default=20, ge=1, le=100, description="ranking 섹션 상위 N개.")
    metrics: list[str] = Field(default_factory=list, description="예약 필드. 현재는 기본 metric 세트를 반환합니다.")
    period_range: DynamicMarketPeriodRange | None = Field(default=None, description="선택 기간 범위.")


class DynamicMarketRequest(BaseModel):
    """Request body for ``POST /api/dynamic-market``."""

    model_config = ConfigDict(extra="forbid")

    filters: DynamicMarketFilters = Field(default_factory=DynamicMarketFilters, description="시장 범위와 차원 narrowing 조건.")
    source: str = Field(default="ubist", description="소스. ubist 또는 iqvia.", examples=["ubist"])
    measure: str = Field(default="sales", description="지표. sales 또는 qty.", examples=["sales"])
    options: DynamicMarketOptions = Field(default_factory=DynamicMarketOptions)
