from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Source = Literal["UBIST", "IQVIA"]
View = Literal["market_landscape", "competitive_dynamics"]
Measure = Literal["sales", "volume", "unit", "dosage_unit", "counting_unit"]

UBIST_MEASURES: list[Measure] = ["sales", "volume"]
IQVIA_MEASURES: list[Measure] = ["sales", "unit", "dosage_unit", "counting_unit"]
VIEWS: list[View] = ["market_landscape", "competitive_dynamics"]


@dataclass(frozen=True)
class DisplayBrand:
    brand_name: str
    market_id: str
    ml_id: str
    source_class: Literal["ubist-only", "iqvia-only", "dual"]
    layer3_aliases: tuple[str, ...] = ()

    @property
    def sources(self) -> list[Source]:
        if self.source_class == "ubist-only":
            return ["UBIST"]
        if self.source_class == "iqvia-only":
            return ["IQVIA"]
        return ["UBIST", "IQVIA"]

    @property
    def default_source(self) -> Source:
        return self.sources[0]

    @property
    def available_measures(self) -> dict[Source, list[Measure]]:
        return {
            source: UBIST_MEASURES if source == "UBIST" else IQVIA_MEASURES
            for source in self.sources
        }

    @property
    def cause_variants(self) -> int:
        return sum(len(measures) for measures in self.available_measures.values()) * len(VIEWS)


DISPLAY_BRANDS: list[DisplayBrand] = [
    DisplayBrand("라베칸", "strategy_001", "ml_001", "ubist-only"),
    DisplayBrand("라베칸듀오", "strategy_001", "ml_001", "ubist-only"),
    DisplayBrand("제이클", "strategy_002", "ml_002", "iqvia-only"),
    DisplayBrand("가드렛", "strategy_003", "ml_003", "dual", ("ANAGLIPTIN",)),
    DisplayBrand("가드메트", "strategy_003", "ml_003", "dual", ("ANAGLIPTIN+METFORMIN",)),
    DisplayBrand("타발리스", "strategy_004", "ml_004", "iqvia-only"),
    DisplayBrand("시그마트", "strategy_005", "ml_005", "ubist-only"),
    DisplayBrand("리바로", "strategy_006", "ml_006", "ubist-only"),
    DisplayBrand("리바로젯", "strategy_006", "ml_006", "ubist-only"),
    DisplayBrand("리바로페노", "strategy_007", "ml_007", "ubist-only"),
    DisplayBrand("리바로하이", "strategy_008", "ml_008", "ubist-only"),
    DisplayBrand("리바로브이", "strategy_008", "ml_008", "ubist-only"),
    DisplayBrand("트루패스", "strategy_009", "ml_009", "ubist-only"),
    DisplayBrand("피나스타", "strategy_009", "ml_009", "ubist-only"),
    DisplayBrand("제이다트", "strategy_009", "ml_009", "ubist-only"),
    DisplayBrand("뉴트로진", "strategy_010", "ml_010", "iqvia-only"),
    DisplayBrand("모빌리아", "strategy_010", "ml_010", "iqvia-only"),
    DisplayBrand("악템라", "strategy_011", "ml_011", "iqvia-only"),
    DisplayBrand("페린젝트", "strategy_012", "ml_012", "iqvia-only"),
    DisplayBrand("베노훼럼", "strategy_012", "ml_012", "iqvia-only"),
    DisplayBrand("헴리브라", "strategy_013", "ml_013", "iqvia-only"),
    DisplayBrand("위너프", "strategy_014", "ml_014", "iqvia-only"),
    DisplayBrand("위너프A+", "strategy_014", "ml_014", "iqvia-only", ("위너프에이플러스",)),
    DisplayBrand("엔커버", "strategy_015", "ml_015", "dual"),
    DisplayBrand("플라주오피", "strategy_016", "ml_016", "iqvia-only"),
]


DISPLAY_BRAND_BY_NAME = {brand.brand_name: brand for brand in DISPLAY_BRANDS}


def get_display_brand(brand_name: str) -> DisplayBrand | None:
    if brand_name in DISPLAY_BRAND_BY_NAME:
        return DISPLAY_BRAND_BY_NAME[brand_name]
    normalized = brand_name.replace(" ", "").lower()
    for brand in DISPLAY_BRANDS:
        if brand.brand_name.replace(" ", "").lower() == normalized:
            return brand
    return None


def validate_source_measure(display_brand: DisplayBrand, source: str, measure: str) -> tuple[bool, str | None]:
    if source not in display_brand.sources:
        return False, "brand_not_in_source"
    available = display_brand.available_measures[source]  # type: ignore[index]
    if measure not in available:
        return False, "invalid_measure_for_source"
    return True, None
