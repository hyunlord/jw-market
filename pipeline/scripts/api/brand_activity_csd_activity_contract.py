from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal, Mapping, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from pipeline.scripts.api.brand_activity_csd_shared import JsonMap, text
from pipeline.scripts.api.brand_activity_topics import JsonValue


CsdEntityLevel = Literal["brand", "company"]
CsdTop5Basis = Literal["iqvia_sales", "csd_market", "activity_count"]

ALLOWED_CHANNELS: Final = ("TOTAL", "GH", "SHPPI", "CPPI", "GH+SHPPI")
ALLOWED_ENTITY_LEVELS: Final = ("brand", "company")
ALLOWED_TOP5_BASES: Final = ("iqvia_sales", "csd_market", "activity_count")
MAX_ENTITIES: Final = 6
MAX_QUARTERS: Final = 12
DEFAULT_QUARTERS: Final = 4
TextChoice = TypeVar("TextChoice", bound=str)

CSD_ACTIVITY_SERIES_EXAMPLE: Final[JsonMap] = {
    "view": "general",
    "market_id": "C10A1",
    "selected_brand": "리바로",
    "filters": {"atc4": ["C10A1"]},
    "entity_level": "brand",
    "csd_channel": "TOTAL",
    "top5_basis": "iqvia_sales",
    "period": {"start": "2024-Q1", "end": "2025-Q4"},
}


class CsdActivitySeriesInputError(RuntimeError):
    """Raised when a CSD activity series request cannot be parsed."""


@dataclass(frozen=True, slots=True)
class ParsedCsdActivityRequest:
    view: str
    market_id: str
    selected_brand: str
    filter_payload: JsonMap
    entity_level: CsdEntityLevel
    csd_channel: str
    top5_basis: CsdTop5Basis
    selected_entities: tuple[str, ...]
    period: JsonMap


class CsdActivitySeriesPeriod(BaseModel):
    """Optional inclusive quarter window for Section 1 CSD activity series."""

    model_config = ConfigDict(extra="ignore")

    start: str | None = Field(default=None, description="포함 시작 분기. 예: 2024-Q1 또는 2024Q1.")
    end: str | None = Field(default=None, description="포함 종료 분기. 예: 2025-Q4 또는 2025Q4.")


class CsdActivitySeriesRequest(BaseModel):
    """Request body for Section 1 CSD Channeldynamics activity series."""

    model_config = ConfigDict(extra="ignore")

    view: str = Field(description="분석 뷰. general 또는 strategic_ml.")
    market_id: str | None = Field(default=None, description="일반뷰 ATC4 또는 전략 ml_id. general은 filters.atc4 첫 값으로 대체 가능.")
    selected_brand: str = Field(description="강조/시장 결정 브랜드.")
    filters: dict[str, JsonValue] = Field(default_factory=dict, description="시장·차원 필터. 신규 계약 필드.")
    filter: dict[str, JsonValue] = Field(default_factory=dict, description="legacy 호환 필드. filters가 있으면 filters가 우선.")
    entity_level: str = Field(default="brand", description="brand 또는 company. company면 representing_company 단위로 활동량을 합산합니다.")
    csd_channel: str = Field(default="TOTAL", description="CSD 원본 jw_channel 값. TOTAL/GH/SHPPI/CPPI/GH+SHPPI.")
    top5_basis: str = Field(default="iqvia_sales", description="iqvia_sales, csd_market, activity_count 중 하나.")
    selected_entities: list[str] = Field(default_factory=list, max_length=MAX_ENTITIES, description="사용자 지정 브랜드/회사 최대 6개. 미지정 시 선택 + top5.")
    period: CsdActivitySeriesPeriod | None = Field(default=None, description="분기 window. 미지정 시 최신 1년, 최대 3년으로 제한.")


def parse_activity_request(payload: Mapping[str, Any]) -> ParsedCsdActivityRequest:
    view = text(payload.get("view"))
    if view not in {"general", "strategic_ml"}:
        raise CsdActivitySeriesInputError(f"unsupported view: {view}")
    filter_payload = _filter_payload(payload)
    market_id = text(payload.get("market_id")) or _first_filter_value(filter_payload, "atc4")
    selected_brand = text(payload.get("selected_brand"))
    if not selected_brand or (view == "general" and not market_id):
        raise CsdActivitySeriesInputError("market_id or filters.atc4, and selected_brand are required")
    period = payload.get("period")
    return ParsedCsdActivityRequest(
        view=view,
        market_id=market_id,
        selected_brand=selected_brand,
        filter_payload=filter_payload,
        entity_level=_typed_value(payload.get("entity_level"), ALLOWED_ENTITY_LEVELS, "entity_level", "brand"),
        csd_channel=_typed_value(payload.get("csd_channel"), ALLOWED_CHANNELS, "csd_channel", "TOTAL"),
        top5_basis=_typed_value(payload.get("top5_basis"), ALLOWED_TOP5_BASES, "top5_basis", "iqvia_sales"),
        selected_entities=_selected_entities(payload.get("selected_entities")),
        period=period if isinstance(period, dict) else {},
    )


def _typed_value(value: Any, allowed: tuple[TextChoice, ...], field: str, default: TextChoice) -> TextChoice:
    candidate = text(value) or default
    if candidate not in allowed:
        raise CsdActivitySeriesInputError(f"unsupported {field}: {candidate}")
    return candidate


def _selected_entities(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        candidate = text(item).strip()
        if candidate and candidate not in seen:
            result.append(candidate)
            seen.add(candidate)
    return tuple(result[:MAX_ENTITIES])


def _filter_payload(payload: Mapping[str, Any]) -> JsonMap:
    filters = payload.get("filters")
    legacy_filter = payload.get("filter")
    if isinstance(filters, dict) and filters:
        return filters
    return legacy_filter if isinstance(legacy_filter, dict) else {}


def _first_filter_value(filter_payload: Mapping[str, Any], key: str) -> str:
    value = filter_payload.get(key)
    if isinstance(value, list):
        return text(value[0]) if value else ""
    return text(value)
