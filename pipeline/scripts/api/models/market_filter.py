from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


MarketFilterView = Literal["general", "strategic"]
MarketFilterSource = Literal["ubist", "iqvia"]
AtcLevel = Literal["atc1", "atc2", "atc3", "atc4"]


class MarketFilterAtcOption(BaseModel):
    """One ATC option node for a hierarchy level."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., description="[프론트] 옵션 식별자. payload에는 이 값을 사용합니다.", examples=["C10A1"])
    level: AtcLevel = Field(..., description="[프론트] ATC 계층 레벨입니다.", examples=["atc4"])
    parent: str | None = Field(
        default=None,
        description="[프론트] 상위 ATC 코드입니다. ATC1은 상위가 없어 null입니다.",
        examples=["C10A"],
    )
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
                        "atc1": [{"key": "C", "level": "atc1", "parent": None, "flag": True}],
                        "atc2": [{"key": "C10", "level": "atc2", "parent": "C", "flag": True}],
                        "atc3": [{"key": "C10A", "level": "atc3", "parent": "C10", "flag": True}],
                        "atc4": [{"key": "C10A1", "level": "atc4", "parent": "C10A", "flag": True}],
                    },
                }
            ]
        },
    )

    brand_name: str = Field(..., description="[입력 echo] 요청 브랜드명.")
    view: MarketFilterView = Field(..., description="[입력 echo] 정규화된 뷰.")
    source: MarketFilterSource = Field(..., description="[입력 echo] 공개 소스. 내부 iqvia_nsa 값은 노출하지 않습니다.")
    market_id: str | None = Field(default=None, description="[프론트 참고] 전략뷰에서 resolve된 ml_id 또는 일반뷰 대표 ATC4.")
    flagged_atc4: list[str] = Field(default_factory=list, description="[프론트] 선택 브랜드가 속한 ATC4 코드 목록.")
    atc: MarketFilterAtcHierarchy = Field(..., description="[프론트] ATC1/2/3/4 옵션 리스트와 flag.")
