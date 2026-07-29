from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pipeline.etl.mi_master_registry import (
    MiMasterRegistry,
    TargetBrand,
    default_mi_master_registry,
)


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


def _source_class(source_type: str) -> Literal["ubist-only", "iqvia-only", "dual"]:
    if source_type == "UBIST":
        return "ubist-only"
    if source_type == "IQVIA":
        return "iqvia-only"
    if source_type == "BOTH":
        return "dual"
    raise ValueError(f"unsupported MI Master source type: {source_type!r}")


def _display_brand(target: TargetBrand) -> DisplayBrand:
    return DisplayBrand(
        brand_name=target.brand_name,
        market_id=target.strategic_market_id,
        ml_id=target.ml_id,
        source_class=_source_class(target.source_type),
        layer3_aliases=target.layer3_aliases,
    )


def build_display_brands(
    registry: MiMasterRegistry | None = None,
) -> list[DisplayBrand]:
    source = registry or default_mi_master_registry()
    return [_display_brand(target) for target in source.target_brands]


DISPLAY_BRANDS = build_display_brands()


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
