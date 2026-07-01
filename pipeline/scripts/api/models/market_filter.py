from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


MarketFilterView = Literal["general", "strategic"]
MarketFilterSource = Literal["ubist", "iqvia", "iqvia_nsa"]
AtcLevel = Literal["atc1", "atc2", "atc3", "atc4"]


class MarketFilterAtcOptionsRequest(BaseModel):
    """Input for the first-step market filter ATC selector."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "brand_name": "리바로",
                    "view": "strategic",
                    "source": "ubist",
                },
                {
                    "brand_name": "가드렛",
                    "view": "general",
                    "source": "iqvia",
                },
            ]
        },
    )

    brand_name: str = Field(
        ...,
        min_length=1,
        description="[입력] 선택 브랜드명. 브랜드가 속한 ATC 항목에 flag=true가 표시됩니다.",
        examples=["리바로"],
    )
    view: MarketFilterView = Field(
        default="strategic",
        description="[입력] 시장필터 1단계 뷰. strategic은 전략시장, general은 일반 ATC 시장입니다.",
        examples=["strategic"],
    )
    source: MarketFilterSource = Field(
        default="ubist",
        description="[입력] 데이터 소스. iqvia는 내부적으로 iqvia_nsa로 정규화됩니다.",
        examples=["ubist"],
    )


class MarketFilterAtcOption(BaseModel):
    """One ATC option node for a hierarchy level."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., description="[프론트] 옵션 식별자. payload에는 이 값을 사용합니다.", examples=["C10A1"])
    value: str = Field(..., description="[프론트] 표시/선택 값. 현재는 ATC 코드와 동일합니다.", examples=["C10A1"])
    label: str = Field(..., description="[프론트] 화면 표시 라벨. 현재는 ATC 코드와 동일합니다.", examples=["C10A1"])
    level: AtcLevel = Field(..., description="[프론트] ATC 계층 레벨입니다.", examples=["atc4"])
    flag: bool = Field(
        default=False,
        description="[프론트] true이면 선택 브랜드가 해당 ATC 노드에 속합니다. 초기 체크/하이라이트 기준입니다.",
        examples=[True],
    )


class MarketFilterAtcHierarchy(BaseModel):
    """ATC1~4 option lists returned by the first-step market filter endpoint."""

    model_config = ConfigDict(extra="forbid")

    atc1: list[MarketFilterAtcOption] = Field(default_factory=list, description="[프론트] ATC1 옵션 리스트.")
    atc2: list[MarketFilterAtcOption] = Field(default_factory=list, description="[프론트] ATC2 옵션 리스트.")
    atc3: list[MarketFilterAtcOption] = Field(default_factory=list, description="[프론트] ATC3 옵션 리스트.")
    atc4: list[MarketFilterAtcOption] = Field(default_factory=list, description="[프론트] ATC4 옵션 리스트.")


class MarketFilterAtcOptionsResponse(BaseModel):
    """First-step market filter response shared with the portal."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "brand_name": "리바로",
                    "view": "strategic",
                    "source": "ubist",
                    "market_id": "ml_006",
                    "flagged_atc4": ["C10A1"],
                    "atc": {
                        "atc1": [{"key": "C", "value": "C", "label": "C", "level": "atc1", "flag": True}],
                        "atc2": [{"key": "C10", "value": "C10", "label": "C10", "level": "atc2", "flag": True}],
                        "atc3": [{"key": "C10A", "value": "C10A", "label": "C10A", "level": "atc3", "flag": True}],
                        "atc4": [{"key": "C10A1", "value": "C10A1", "label": "C10A1", "level": "atc4", "flag": True}],
                    },
                }
            ]
        },
    )

    brand_name: str = Field(..., description="[입력 echo] 요청 브랜드명.")
    view: MarketFilterView = Field(..., description="[입력 echo] 정규화된 뷰.")
    source: str = Field(..., description="[입력 echo] 정규화된 소스. IQVIA는 iqvia_nsa로 반환됩니다.")
    market_id: str | None = Field(default=None, description="[프론트 참고] 전략뷰에서 resolve된 ml_id 또는 일반뷰 대표 ATC4.")
    flagged_atc4: list[str] = Field(default_factory=list, description="[프론트] 선택 브랜드가 속한 ATC4 코드 목록.")
    atc: MarketFilterAtcHierarchy = Field(..., description="[프론트] ATC1/2/3/4 옵션 리스트와 flag.")
