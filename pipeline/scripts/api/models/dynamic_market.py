"""Request schemas for the dynamic market route."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from pipeline.scripts.api.dynamic_market.channel_axis import ChannelAxisFilter, ChannelAxisPair


class UbistChannelAxisPair(BaseModel):
    """Raw UBIST facility-specialty pair selected as one OR item."""

    model_config = ConfigDict(extra="forbid")

    facility: str = Field(description="UBIST 원천 종별 값.", examples=["종합병원"])
    specialty: str = Field(description="UBIST 원천 진료과 값.", examples=["순환기(Cardiology IM)"])


class UbistAnalysisLevel(BaseModel):
    """UBIST product-level dynamic filters from the mock OpenAPI contract."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    seller: list[str] = Field(default_factory=list, description="판매사 필터. 값 목록 안에서는 OR로 결합합니다.", examples=[["JW중외제약"]])
    molecule: list[str] = Field(
        default_factory=list,
        description="UBIST 원천 성분 문자열 필터. 복합 성분도 분해하지 않고 원문 한 값으로 취급하며 값 목록 안에서는 OR로 결합합니다.",
        examples=[["PITAVASTATIN / EZETIMIBE"]],
    )
    molecule_strength: list[str] = Field(default_factory=list, description="성분용량 필터.", examples=[["PITAVASTATIN 2MG"]])
    form: list[str] = Field(default_factory=list, description="제형 필터.", examples=[["정제"]])
    route: list[str] = Field(default_factory=list, description="투여경로 필터.", examples=[["경구"]])
    reimbursement: list[str] = Field(default_factory=list, description="급여구분 필터.", examples=[["급여"]])
    atc3: list[str] = Field(default_factory=list, description="ATC3 narrowing 필터.", examples=[["C10A"]])
    atc4: list[str] = Field(default_factory=list, description="ATC4 narrowing 필터.", examples=[["C10A1"]])
    facility: list[str] = Field(default_factory=list, description="UBIST 종별 값 슬라이스.", examples=[["종합병원"]])
    specialty: list[str] = Field(default_factory=list, description="UBIST 진료과 값 슬라이스.", examples=[["순환기(Cardiology IM)"]])
    pairs: list[UbistChannelAxisPair] = Field(default_factory=list, description="UBIST 종별×진료과 pair 값 슬라이스.")


class IqviaAnalysisLevel(BaseModel):
    """IQVIA product-level dynamic filters from the mock OpenAPI contract."""

    model_config = ConfigDict(extra="forbid")

    mfr_name_kor: list[str] = Field(default_factory=list, description="IQVIA 제조사 한글명 필터.", examples=[["JW중외제약"]])
    molecule_type: list[str] = Field(default_factory=list, description="IQVIA molecule type 필터.")
    molecule_desc: list[str] = Field(default_factory=list, description="IQVIA molecule desc 성분 필터.")
    dosage_form: list[str] = Field(default_factory=list, description="IQVIA NFC dosage form 필터.")
    pack_desc: list[str] = Field(default_factory=list, description="IQVIA pack desc 필터.")
    strength: list[str] = Field(default_factory=list, description="IQVIA strength 필터.")
    nhi_type: list[str] = Field(default_factory=list, description="IQVIA NHI type 필터.")
    audit_code: list[str] = Field(default_factory=list, description="IQVIA audit code 값 슬라이스. 비어 있으면 전체 audit matrix를 포함합니다.")


class DynamicMarketAnalysisLevel(BaseModel):
    """Source-specific analysis-level filters.

    UBIST and IQVIA dimensions are deliberately not mapped together; the
    selected ``source`` determines which nested object is accepted.
    """

    model_config = ConfigDict(extra="forbid")

    ubist: UbistAnalysisLevel = Field(default_factory=UbistAnalysisLevel)
    iqvia: IqviaAnalysisLevel = Field(default_factory=IqviaAnalysisLevel)

    def to_dimension_payload(self, *, source: str) -> dict[str, dict[str, list[str]]]:
        """Return only row-filter dimensions; value-slice fields are removed."""

        source_key = _api_source_key(source)
        payload = self.model_dump(by_alias=True)
        if source_key == "ubist":
            payload["ubist"].pop("facility", None)
            payload["ubist"].pop("specialty", None)
            payload["ubist"].pop("pairs", None)
        else:
            payload["iqvia"].pop("audit_code", None)
        return payload

    def to_channel_axis(self, *, source: str) -> ChannelAxisFilter | None:
        """Convert integrated analysis_level value-slice fields to runtime channel-axis filters."""

        normalized_source = _normalize_source(source)
        source_key = _api_source_key(normalized_source)
        ubist_active = bool(
            [value for value in self.ubist.facility if value.strip()]
            or [value for value in self.ubist.specialty if value.strip()]
            or [item for item in self.ubist.pairs if item.facility.strip() and item.specialty.strip()]
        )
        iqvia_active = bool([value for value in self.iqvia.audit_code if value.strip()])
        if ubist_active and source_key != "ubist":
            raise ValueError("analysis_level.ubist facility/specialty filters must match selected source")
        if iqvia_active and source_key != "iqvia":
            raise ValueError("analysis_level.iqvia.audit_code must match selected source")
        if source_key == "ubist":
            pairs = tuple(
                ChannelAxisPair(facility=item.facility.strip(), specialty=item.specialty.strip())
                for item in self.ubist.pairs
                if item.facility.strip() and item.specialty.strip()
            )
            selected = ChannelAxisFilter(
                source="ubist",
                facilities=tuple(dict.fromkeys(value.strip() for value in self.ubist.facility if value.strip())),
                specialties=tuple(dict.fromkeys(value.strip() for value in self.ubist.specialty if value.strip())),
                pairs=pairs,
            )
            return selected if selected.is_active else None
        selected = ChannelAxisFilter(
            source=normalized_source,
            audit_codes=tuple(dict.fromkeys(value.strip().upper() for value in self.iqvia.audit_code if value.strip())),
        )
        return selected if selected.is_active else None


class StrategicAtcAnalysisLevel(BaseModel):
    """Internal strategic ATC narrowing, populated from top-level filters.atc4."""

    model_config = ConfigDict(extra="forbid")

    atc3: list[str] = Field(default_factory=list)
    atc4: list[str] = Field(default_factory=list)


class DynamicMarketAnalysisLevelFilters(BaseModel):
    """Internal source-specific strategic narrowing contract."""

    model_config = ConfigDict(extra="forbid")

    ubist: StrategicAtcAnalysisLevel = Field(default_factory=StrategicAtcAnalysisLevel)
    iqvia: StrategicAtcAnalysisLevel = Field(default_factory=StrategicAtcAnalysisLevel)


class UbistChannelAxis(BaseModel):
    """UBIST value-slice filters over facility x specialty raw matrix."""

    model_config = ConfigDict(extra="forbid")

    facility: list[str] = Field(default_factory=list, description="UBIST 종별 OR 선택. specialty와 함께 보내면 두 축은 AND로 결합합니다.")
    specialty: list[str] = Field(default_factory=list, description="UBIST 진료과 OR 선택. facility와 함께 보내면 두 축은 AND로 결합합니다.")
    pairs: list[UbistChannelAxisPair] = Field(default_factory=list, description="종별×진료과 raw pair OR 선택. 있으면 pair 선택이 우선합니다.")


class IqviaChannelAxis(BaseModel):
    """IQVIA audit-code value-slice filters over the raw audit matrix."""

    model_config = ConfigDict(extra="forbid")

    audit_code: list[str] = Field(default_factory=list, description="IQVIA audit_code OR 선택.")


class DynamicMarketChannelAxis(BaseModel):
    """Source-specific value-slice filters, separate from analysis_level row filters."""

    model_config = ConfigDict(extra="forbid")

    ubist: UbistChannelAxis = Field(default_factory=UbistChannelAxis)
    iqvia: IqviaChannelAxis = Field(default_factory=IqviaChannelAxis)

    def to_filter(self, *, source: str = "ubist") -> ChannelAxisFilter | None:
        """Convert the API boundary model into the runtime slice contract."""

        normalized_source = source.strip().lower()
        if normalized_source == "iqvia":
            normalized_source = "iqvia_nsa"
        source_key = "iqvia" if normalized_source == "iqvia_nsa" else normalized_source
        ubist_active = bool(
            [value for value in self.ubist.facility if value.strip()]
            or [value for value in self.ubist.specialty if value.strip()]
            or [item for item in self.ubist.pairs if item.facility.strip() and item.specialty.strip()]
        )
        iqvia_active = bool([value for value in self.iqvia.audit_code if value.strip()])
        if ubist_active and source_key != "ubist":
            raise ValueError("channel_axis.ubist must match selected source")
        if iqvia_active and source_key != "iqvia":
            raise ValueError("channel_axis.iqvia must match selected source")
        if normalized_source == "ubist":
            pairs = tuple(
                ChannelAxisPair(facility=item.facility.strip(), specialty=item.specialty.strip())
                for item in self.ubist.pairs
                if item.facility.strip() and item.specialty.strip()
            )
            selected = ChannelAxisFilter(
                source="ubist",
                facilities=tuple(dict.fromkeys(value.strip() for value in self.ubist.facility if value.strip())),
                specialties=tuple(dict.fromkeys(value.strip() for value in self.ubist.specialty if value.strip())),
                pairs=pairs,
            )
            return selected if selected.is_active else None
        selected = ChannelAxisFilter(
            source=normalized_source,
            audit_codes=tuple(dict.fromkeys(value.strip().upper() for value in self.iqvia.audit_code if value.strip())),
        )
        return selected if selected.is_active else None


class DynamicMarketFilters(BaseModel):
    """Filter set accepted by general and strategic dynamic resolvers."""

    model_config = ConfigDict(extra="forbid")

    atc4: list[str] = Field(
        default_factory=list,
        description="공통 ATC4 OR 범위. 일반뷰는 scope, 전략뷰는 ML/CD 내부 narrowing으로 사용합니다.",
        examples=[["C10A1", "C10C0"]],
    )
    view_kind: str | None = Field(
        default=None,
        description="Deprecated legacy 전략뷰 힌트. 신규 호출은 top-level view(strategic_ml/strategic_cd)를 사용합니다.",
        examples=["market_landscape"],
    )
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

    period_range: DynamicMarketPeriodRange | None = Field(default=None, description="선택 기간 범위.")


class DynamicMarketRequest(BaseModel):
    """Request body for ``POST /api/dynamic-market``."""

    model_config = ConfigDict(extra="forbid")

    view: str | None = Field(
        default=None,
        description=(
            "명시적 뷰 키. general, strategic_ml, strategic_cd 중 하나입니다. "
            "생략 시 기존 filters.view_kind 기반 추론을 유지하지만 deprecated 예정입니다."
        ),
        examples=["general"],
    )
    filters: DynamicMarketFilters = Field(default_factory=DynamicMarketFilters, description="시장 범위와 차원 narrowing 조건.")
    source: str = Field(default="ubist", description="소스. ubist 또는 iqvia.", examples=["ubist"])
    measure: str = Field(default="sales", description="지표. sales 또는 qty.", examples=["sales"])
    options: DynamicMarketOptions = Field(default_factory=DynamicMarketOptions)


def _normalize_source(source: str) -> str:
    normalized = source.strip().lower()
    if normalized in {"iqvia", "nsa"}:
        return "iqvia_nsa"
    return normalized


def _api_source_key(source: str) -> str:
    return "iqvia" if _normalize_source(source) == "iqvia_nsa" else _normalize_source(source)
