from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AtcFilter(BaseModel):
    model_config = ConfigDict(extra="allow")

    atc3: list[str] = Field(default_factory=list, description="ATC3 코드 멀티 선택(OR). 예: ['C10A']")
    atc4: list[str] = Field(default_factory=list, description="ATC4 코드 멀티 선택(OR). 예: ['C10A1']")


class UbistAnalysisLevel(BaseModel):
    """UBIST 분석레벨. 각 차원 멀티(차원 내 OR), 차원 간 AND."""

    model_config = ConfigDict(extra="allow")

    seller: list[str] = Field(default_factory=list, description='판매사(제약사) 멀티선택. [입력] 예 ["JW중외제약","한미약품"]')
    molecule: list[str] = Field(default_factory=list, description='성분(주성분) 멀티선택. [입력] 예 ["PITAVASTATIN"].')
    molecule_strength: list[str] = Field(default_factory=list, description='성분용량 멀티선택. [입력] 예 ["PITAVASTATIN 2mg"].')
    form: list[str] = Field(default_factory=list, description='제형 멀티선택. [입력] 예 ["정제","캡슐"]')
    route: list[str] = Field(default_factory=list, description='투여경로 멀티선택. [입력] 예 ["경구","주사"]')
    reimbursement: list[str] = Field(default_factory=list, description='급여구분 멀티선택. [입력] 예 ["급여","비급여"]')


class IqviaAnalysisLevel(BaseModel):
    """IQVIA 분석레벨. 각 차원 멀티(차원 내 OR), 차원 간 AND."""

    model_config = ConfigDict(extra="allow")

    mfr_name_kor: list[str] = Field(default_factory=list, description='제조사명(한글) 멀티선택. [입력] 예 ["제이더블유중외제약"]')
    molecule_type: list[str] = Field(default_factory=list, description='성분 타입 멀티선택. [입력] 예 ["SINGLE","COMBINE"]')
    molecule_desc: list[str] = Field(default_factory=list, description='성분명(IQVIA 표기) 멀티선택. [입력] 예 ["PITAVASTATIN"]')
    pack_desc: list[str] = Field(default_factory=list, description='팩(제형·포장 단위) 설명 멀티선택. [입력] 예 ["TAB 30s"].')
    strength: list[str] = Field(default_factory=list, description='함량 멀티선택. [입력] 예 ["2MG"].')
    nhi_type: list[str] = Field(default_factory=list, description='급여구분(NHI) 멀티선택. [입력] 예 ["급여","비급여"]')


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


class ChannelFilter(BaseModel):
    """PPTX p4 채널 필터. UBIST는 종별/진료과, IQVIA는 AUDIT CODE를 사용한다."""

    model_config = ConfigDict(extra="allow")

    visit_location: list[str] = Field(default_factory=list, description="종별(상급종병/종병/병원/의원 등) 멀티선택.")
    specialty: list[str] = Field(default_factory=list, description='진료과 멀티선택. [입력] 예 ["내과","순환기내과"].')
    audit_code: list[str] = Field(default_factory=list, description="IQVIA AUDIT CODE 멀티선택.")


class MarketFilter(BaseModel):
    model_config = ConfigDict(extra="allow")

    atc: AtcFilter = Field(
        default_factory=AtcFilter,
        description="ATC 코드 필터. [프론트] ATC 계층 트리/멀티선택 UI. atc3·atc4 멀티 입력.",
    )
    analysis_level: AnalysisLevel = Field(
        default_factory=AnalysisLevel,
        description="분석레벨 필터. [프론트] UBIST/IQVIA 소스별 탭 분리, 각 차원 멀티선택. 차원 내 OR·차원 간 AND.",
    )
    channel: ChannelFilter = Field(
        default_factory=ChannelFilter,
        description="채널 필터. [프론트] 종별/진료과/AUDIT CODE 멀티선택. 데이터를 채널 단위로 좁힘.",
    )
    channel_axis: dict[str, Any] = Field(
        default_factory=dict,
        description='동적 시장 API와 같은 value-slice 채널 축 필터. 예: {"iqvia":{"audit_code":["KPA"]}}.',
    )


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
    channel_axis: dict[str, Any] = Field(
        default_factory=dict,
        description='Top-level legacy 채널 축 필터. 있으면 filters.channel_axis로 병합된다. 예: {"iqvia":{"audit_code":["KPA","KHPA"]}}.',
    )


class CsdTimeseriesRequest(BrandActivityBaseRequest):
    """Request body for the Brand Activity integrated CSD timeseries route."""

    mode: str = Field(
        "absolute",
        description="추세 차트 표현 방식. [입력] absolute=절대값, share=시장 총합 대비 점유율.",
    )
    window: CsdTimeseriesWindow | None = Field(default=None, description="선택 조회 기간. 미지정 시 서버 기본 기간.")


class BrandActivityTopicsRequest(BrandActivityBaseRequest):
    """Request body for the filtered Brand Activity topic route."""

    visit_location: str | list[str] = Field("전체", description="종별 shortcut. 문자열 또는 OR 리스트이며 filters.channel.visit_location과 병행 호환.")
    specialty: str | list[str] = Field("전체", description="진료과 shortcut. 문자열 또는 OR 리스트이며 filters.channel.specialty와 병행 호환.")
    interest: str | list[str] = Field("전체", description="키워드 유용성 shortcut. 문자열 또는 OR 리스트.")
    prescription_evolution: str | list[str] = Field("전체", description="처방 변화 shortcut. 문자열 또는 OR 리스트.")
    period_start: str | None = Field(default=None, description="키워드 집계 시작월 YYYY-MM.")
    period_end: str | None = Field(default=None, description="키워드 집계 종료월 YYYY-MM.")
    top_n: int = Field(default=5, ge=1, le=10, description="브랜드 카드에 보여줄 상위 토픽 개수. [입력] 1~10, 기본 5.")


class InterestRxWeights(BaseModel):
    """Optional score-weight overrides for interest/Rx matrix axes."""

    model_config = ConfigDict(extra="ignore")

    interest: dict[str, float] = Field(default_factory=dict, description="관심도 범주별 score 가중치 override.")
    rx_frequency: dict[str, float] = Field(default_factory=dict, description="처방빈도 범주별 score 가중치 override.")
    prescription_evolution: dict[str, float] = Field(default_factory=dict, description="처방 변화 범주별 score 가중치 override.")


class BrandActivityInterestRxRequest(BrandActivityBaseRequest):
    """Request body for the Brand Activity interest/Rx matrix route."""

    visit_location: str = Field("전체", description="종별 단일 선택 shortcut. filters.channel.visit_location과 병행 호환.")
    specialty: str = Field("전체", description="진료과 단일 선택 shortcut. filters.channel.specialty와 병행 호환.")
    period_start: str | None = Field(default=None, description="조회 시작 월. 예: 2024-01.")
    period_end: str | None = Field(default=None, description="조회 종료 월. 예: 2025-12.")
    weights: InterestRxWeights | None = Field(
        default=None,
        description="범주별 가중치. 미지정 시 서버 기본 가중치로 score를 계산.",
    )
