from __future__ import annotations

import re
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _dict(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _rename(data: dict[str, Any], source: str, target: str) -> None:
    # camelCase names are private BFF compatibility aliases and stay out of public OpenAPI.
    if source in data and target not in data:
        data[target] = data.pop(source)


def _none_to_empty_lists(data: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        if data.get(key) is None:
            data[key] = []


class AtcFilter(BaseModel):
    model_config = ConfigDict(extra="allow")

    atc4: list[str] = Field(default_factory=list, description="ATC4 코드 멀티 선택(OR). 예: ['C10A1']")

    @model_validator(mode="before")
    @classmethod
    def normalize_null_lists(cls, value: Any) -> Any:
        data = _dict(value)
        if data is None:
            return value
        _none_to_empty_lists(data, ("atc4",))
        return data


class UbistAnalysisLevel(BaseModel):
    """UBIST 분석레벨. 각 차원 멀티(차원 내 OR), 차원 간 AND."""

    model_config = ConfigDict(extra="allow")

    seller: list[str] = Field(default_factory=list, description='판매사(제약사) 멀티선택. [입력] 예 ["JW중외제약","한미약품"]')
    molecule: list[str] = Field(default_factory=list, description='성분(주성분) 멀티선택. [입력] 예 ["PITAVASTATIN"].')
    molecule_strength: list[str] = Field(default_factory=list, description='성분용량 멀티선택. [입력] 예 ["PITAVASTATIN 2mg"].')
    form: list[str] = Field(default_factory=list, description='제형 멀티선택. [입력] 예 ["정제","캡슐"]')
    route: list[str] = Field(default_factory=list, description='투여경로 멀티선택. [입력] 예 ["경구","주사"]')
    reimbursement: list[str] = Field(default_factory=list, description='급여구분 멀티선택. [입력] 예 ["급여","비급여"]')

    @model_validator(mode="before")
    @classmethod
    def normalize_bff_keys(cls, value: Any) -> Any:
        data = _dict(value)
        if data is None:
            return value
        _rename(data, "moleculeStrength", "molecule_strength")
        _none_to_empty_lists(data, ("seller", "molecule", "molecule_strength", "form", "route", "reimbursement"))
        return data


class IqviaAnalysisLevel(BaseModel):
    """IQVIA 분석레벨. 각 차원 멀티(차원 내 OR), 차원 간 AND."""

    model_config = ConfigDict(extra="allow")

    mfr_name_kor: list[str] = Field(default_factory=list, description='제조사명(한글) 멀티선택. [입력] 예 ["제이더블유중외제약"]')
    molecule_type: list[str] = Field(default_factory=list, description='성분 타입 멀티선택. [입력] 예 ["SINGLE","COMBINE"]')
    molecule_desc: list[str] = Field(default_factory=list, description='성분명(IQVIA 표기) 멀티선택. [입력] 예 ["PITAVASTATIN"]')
    pack_desc: list[str] = Field(default_factory=list, description='팩(제형·포장 단위) 설명 멀티선택. [입력] 예 ["TAB 30s"].')
    strength: list[str] = Field(default_factory=list, description='함량 멀티선택. [입력] 예 ["2MG"].')
    nhi_type: list[str] = Field(default_factory=list, description='급여구분(NHI) 멀티선택. [입력] 예 ["급여","비급여"]')
    audit_code: list[str] = Field(
        default_factory=list,
        description='IQVIA Audit Code(채널) 멀티선택. 비어 있으면 전체 Audit Code를 포함합니다. [입력] 예 ["KHPA"].',
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_bff_keys(cls, value: Any) -> Any:
        data = _dict(value)
        if data is None:
            return value
        for source, target in (
            ("mfrNameKor", "mfr_name_kor"),
            ("moleculeType", "molecule_type"),
            ("moleculeDesc", "molecule_desc"),
            ("packDesc", "pack_desc"),
            ("nhiType", "nhi_type"),
            ("auditCode", "audit_code"),
        ):
            _rename(data, source, target)
        _none_to_empty_lists(data, ("mfr_name_kor", "molecule_type", "molecule_desc", "pack_desc", "strength", "nhi_type", "audit_code"))
        return data


class AnalysisLevel(BaseModel):
    """분석레벨. UBIST/IQVIA 소스별 차원이 다르므로 데이터 소스에 맞는 쪽을 사용한다."""

    model_config = ConfigDict(extra="allow")

    ubist: UbistAnalysisLevel = Field(
        default_factory=UbistAnalysisLevel,
        description="UBIST 소스 분석레벨(판매사/성분/제형/투여경로/급여구분 등). [프론트] UBIST 탭 하위 멀티선택 그룹.",
    )
    iqvia: IqviaAnalysisLevel = Field(
        default_factory=IqviaAnalysisLevel,
        description="IQVIA 소스 분석레벨(MFR/MOLECULE/STRENGTH/NHI). [프론트] IQVIA 탭 하위 멀티선택 그룹.",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_null_sources(cls, value: Any) -> Any:
        data = _dict(value)
        if data is None:
            return value
        for key in ("ubist", "iqvia"):
            if data.get(key) is None:
                data[key] = {}
        return data


class ChannelFilter(BaseModel):
    """Legacy IQVIA audit-code shortcut."""

    model_config = ConfigDict(extra="allow")

    audit_code: list[str] = Field(default_factory=list, description="IQVIA AUDIT CODE 멀티선택.")

    @model_validator(mode="before")
    @classmethod
    def normalize_bff_keys(cls, value: Any) -> Any:
        data = _dict(value)
        if data is None:
            return value
        _rename(data, "auditCode", "audit_code")
        _none_to_empty_lists(data, ("audit_code",))
        return data


class MarketScopeFilter(BaseModel):
    """Optional catalog market-scope member filter for Brand Activity."""

    model_config = ConfigDict(extra="ignore")

    option_id: str = Field(description='Market-scope catalog option id. 예: "group:livalo_family".')
    member: str | None = Field(
        default=None,
        description='선택할 group member 브랜드명. Phase 1에서는 특정 member만 지원하며 "전체"/미지정은 400입니다.',
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_bff_keys(cls, value: Any) -> Any:
        data = _dict(value)
        if data is None:
            return value
        _rename(data, "optionId", "option_id")
        return data


class MarketFilter(BaseModel):
    model_config = ConfigDict(extra="allow")

    atc: AtcFilter = Field(
        default_factory=AtcFilter,
        description="ATC 코드 필터. [프론트] ATC4 멀티선택 UI.",
    )
    analysis_level: AnalysisLevel = Field(
        default_factory=AnalysisLevel,
        description="분석레벨 필터. [프론트] UBIST/IQVIA 소스별 탭 분리, 각 차원 멀티선택. 차원 내 OR·차원 간 AND.",
    )
    channel: ChannelFilter = Field(
        default_factory=ChannelFilter,
        description="Legacy IQVIA Audit Code shortcut. 신규 호출은 analysis_level.iqvia.audit_code를 사용합니다.",
    )
    market_scope: MarketScopeFilter | None = Field(
        default=None,
        description="group:* 시장군의 특정 member만 선택하는 필터. Phase 1은 general view에서 member 단일 선택만 지원합니다.",
    )

    @model_validator(mode="before")
    @classmethod
    def reject_public_channel_axis(cls, value: Any) -> Any:
        data = _dict(value)
        if data is None:
            return value
        if "channel_axis" in data:
            raise ValueError("channel_axis has moved to filters.analysis_level.<source>")
        _rename(data, "analysisLevel", "analysis_level")
        _rename(data, "marketScope", "market_scope")
        for key in ("atc", "analysis_level", "channel"):
            if data.get(key) is None:
                data[key] = {}
        return data


class CsdTimeseriesWindow(BaseModel):
    """Optional inclusive quarter window for Brand Activity CSD timeseries."""

    model_config = ConfigDict(extra="ignore")

    start: str | None = Field(default=None, description="조회 시작 분기 또는 월. 예: 2024-Q1, 2024-01.")
    end: str | None = Field(default=None, description="조회 종료 분기 또는 월. 예: 2025-Q4, 2025-12.")


class BrandActivityBaseRequest(BaseModel):
    """Shared Brand Activity request fields."""

    model_config = ConfigDict(extra="ignore")

    view: str = Field(
        "general",
        description="분석 뷰. [입력] general=일반뷰, strategic_ml=전략뷰-시장조망, strategic_cd=전략뷰-경쟁구도.",
    )
    selected_brand: str = Field(..., description="선택 브랜드. [프론트] 강조 대상이며 전략뷰에서는 시장 결정자.")
    filters: MarketFilter = Field(
        default_factory=MarketFilter,
        description="시장 필터(ATC+분석레벨+채널). 차원 내 OR, 차원 간 AND. 기존 flat dict extra 필드도 호환.",
    )
    filter: MarketFilter = Field(
        default_factory=MarketFilter,
        description="Legacy 단수 필터 입력. 신규 호출은 filters를 사용하되 기존 클라이언트 호환을 위해 유지.",
    )

    @model_validator(mode="before")
    @classmethod
    def reject_public_channel_axis(cls, value: Any) -> Any:
        data = _dict(value)
        if data is None:
            return value
        if "channel_axis" in data:
            raise ValueError("channel_axis has moved to filters.analysis_level.<source>")
        _rename(data, "selectedBrand", "selected_brand")
        for key in ("filters", "filter"):
            if data.get(key) is None:
                data[key] = {}
        return data


class CsdTimeseriesRequest(BrandActivityBaseRequest):
    """Request body for the Brand Activity integrated CSD timeseries route."""

    market_id: str | None = Field(default=None, description="전략뷰 다중 시장 소속을 명시적으로 선택하는 ml_id 또는 cd_id.")
    mode: str = Field(
        "absolute",
        description="추세 차트 표현 방식. [입력] absolute=절대값, share=시장 총합 대비 점유율.",
    )
    window: CsdTimeseriesWindow | None = Field(default=None, description="선택 조회 기간. 미지정 시 서버 기본 기간.")

    @model_validator(mode="before")
    @classmethod
    def normalize_null_mode(cls, value: Any) -> Any:
        data = _dict(value)
        if data is None:
            return value
        if data.get("mode") is None:
            data.pop("mode", None)
        return data


class BrandActivityTopicsRequest(BrandActivityBaseRequest):
    """Request body for the filtered Brand Activity topic route."""

    market_id: str | None = Field(default=None, description="전략뷰 다중 시장 소속을 명시적으로 선택하는 ml_id 또는 cd_id.")
    visit_location: str | list[str] = Field("전체", description="종별 shortcut. 문자열 또는 OR 리스트.")
    specialty: str | list[str] = Field("전체", description="진료과 shortcut. 문자열 또는 OR 리스트.")
    interest: str | list[str] = Field("전체", description="키워드 유용성 shortcut. 문자열 또는 OR 리스트.")
    prescription_evolution: str | list[str] = Field("전체", description="처방 변화 shortcut. 문자열 또는 OR 리스트.")
    start_date: str | None = Field(default=None, description="키워드 집계 시작월 YYYY-MM.")
    end_date: str | None = Field(default=None, description="키워드 집계 종료월 YYYY-MM.")
    period_start: str | None = Field(default=None, description="키워드 집계 시작월 YYYY-MM.")
    period_end: str | None = Field(default=None, description="키워드 집계 종료월 YYYY-MM.")
    top_n: int = Field(default=5, ge=1, le=10, description="브랜드 카드에 보여줄 상위 토픽 개수. [입력] 1~10, 기본 5.")

    @model_validator(mode="before")
    @classmethod
    def normalize_bff_keys(cls, value: Any) -> Any:
        data = _dict(value)
        if data is None:
            return value
        for source, target in (("topN", "top_n"), ("periodStart", "period_start"), ("periodEnd", "period_end")):
            _rename(data, source, target)
        for canonical, legacy in (("start_date", "period_start"), ("end_date", "period_end")):
            if data.get(canonical) is not None and data.get(legacy) is not None and data[canonical] != data[legacy]:
                raise ValueError(f"{canonical} and {legacy} must match when both are provided")
            value = data.get(canonical) if data.get(canonical) is not None else data.get(legacy)
            if value is not None:
                data[canonical] = value
                data[legacy] = value
        if data.get("top_n") is None:
            data.pop("top_n", None)
        return data

    @field_validator("start_date", "end_date", "period_start", "period_end")
    @classmethod
    def validate_month_format(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value) is None:
            raise ValueError("month must use YYYY-MM format")
        return value

    @model_validator(mode="after")
    def validate_period_order(self) -> BrandActivityTopicsRequest:
        if self.start_date is not None and self.end_date is not None and self.start_date > self.end_date:
            raise ValueError("start_date must be earlier than or equal to end_date")
        return self


class InterestRxWeights(BaseModel):
    """Optional score-weight overrides for interest/Rx matrix axes."""

    model_config = ConfigDict(extra="ignore")

    interest: dict[str, float] = Field(default_factory=dict, description="관심도 범주별 score 가중치 override.")
    rx_frequency: dict[str, float] = Field(default_factory=dict, description="처방빈도 범주별 score 가중치 override.")
    prescription_evolution: dict[str, float] = Field(default_factory=dict, description="처방 변화 범주별 score 가중치 override.")


class BrandActivityInterestRxRequest(BrandActivityBaseRequest):
    """Request body for the Brand Activity interest/Rx matrix route."""

    market_id: str | None = Field(default=None, description="전략뷰 다중 시장 소속을 명시적으로 선택하는 ml_id 또는 cd_id.")
    visit_location: str = Field("전체", description="종별 단일 선택 shortcut.")
    specialty: str = Field("전체", description="진료과 단일 선택 shortcut.")
    period_start: str | None = Field(default=None, description="조회 시작 월. 예: 2024-01.")
    period_end: str | None = Field(default=None, description="조회 종료 월. 예: 2025-12.")
    weights: InterestRxWeights | None = Field(
        default=None,
        description="범주별 가중치. 미지정 시 서버 기본 가중치로 score를 계산.",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_bff_keys(cls, value: Any) -> Any:
        data = _dict(value)
        if data is None:
            return value
        for source, target in (("periodStart", "period_start"), ("periodEnd", "period_end"), ("visitLocation", "visit_location")):
            _rename(data, source, target)
        return data
