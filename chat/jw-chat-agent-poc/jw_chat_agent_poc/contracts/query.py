from __future__ import annotations

from enum import StrEnum
import hashlib
import json
from typing import Any, Final

from pydantic import BaseModel, Field, model_validator

from .base import ContractModel


class EntityKind(StrEnum):
    BRAND = "brand"
    MARKET = "market"


class PortalMarketView(StrEnum):
    MARKET_LANDSCAPE = "market_landscape"
    COMPETITIVE_DYNAMICS = "competitive_dynamics"


class MarketSource(StrEnum):
    UBIST = "UBIST"
    IQVIA = "IQVIA"


class NativeMarketMeasure(StrEnum):
    SALES = "sales"
    VOLUME = "volume"
    UNIT = "unit"
    DOSAGE_UNIT = "dosage_unit"
    COUNTING_UNIT = "counting_unit"


class MeasureKind(StrEnum):
    ABSOLUTE = "absolute"
    RATIO = "ratio"
    RANK = "rank"
    COUNT = "count"


class PeriodGranularity(StrEnum):
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class CatalogSourceClass(StrEnum):
    UBIST_ONLY = "ubist-only"
    IQVIA_ONLY = "iqvia-only"
    DUAL = "dual"


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class ParameterStatus(StrEnum):
    VALID = "valid"
    UNSUPPORTED_COMBINATION = "unsupported_combination"
    NOT_APPLICABLE = "not_applicable"
    UNRESOLVED = "unresolved"


class ParameterIssueReason(StrEnum):
    INVALID_MEASURE_FOR_SOURCE = "invalid_measure_for_source"
    BRAND_NOT_IN_SOURCE = "brand_not_in_source"


class EntityRef(ContractModel):
    kind: EntityKind
    canonical_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    market_id: str | None = None


class MeasureSpec(ContractModel):
    kind: MeasureKind
    name: str = Field(min_length=1)


class PeriodSpec(ContractModel):
    start: str | None = None
    end: str | None = None
    granularity: PeriodGranularity | None = None


class UnitSpec(ContractModel):
    code: str = Field(min_length=1)
    label: str = Field(min_length=1)


class SourceMeasureCapability(ContractModel):
    source: MarketSource
    native_measures: tuple[NativeMarketMeasure, ...]


class BrandCapabilitySnapshot(ContractModel):
    entity: EntityRef
    source_class: CatalogSourceClass
    source_capabilities: tuple[SourceMeasureCapability, ...]
    catalog_snapshot_id: str = Field(min_length=1)


class ParameterResolution(ContractModel):
    resolution_status: ResolutionStatus
    status: ParameterStatus
    reason: ParameterIssueReason | None = None
    requested_source: MarketSource
    requested_native_measure: NativeMarketMeasure
    valid_measures: tuple[NativeMarketMeasure, ...] = ()


_AXIS_ID_FIELDS: Final[tuple[str, ...]] = (
    "market_id",
    "market_definition",
    "view",
    "source",
    "native_measure",
    "measure",
    "period",
    "unit",
    "catalog_snapshot_id",
    "market_definition_version",
)


def _axis_identity(values: dict[str, Any]) -> str:
    payload: dict[str, Any] = {}
    for field_name in _AXIS_ID_FIELDS:
        value = values.get(field_name)
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        elif isinstance(value, StrEnum):
            value = value.value
        payload[field_name] = value
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MarketAxisSpec(ContractModel):
    market_id: str = Field(min_length=1)
    market_definition: str = Field(min_length=1)
    view: PortalMarketView
    source: MarketSource
    native_measure: NativeMarketMeasure
    measure: MeasureSpec
    period: PeriodSpec
    unit: UnitSpec
    catalog_snapshot_id: str = Field(min_length=1)
    market_definition_version: str = Field(min_length=1)
    axis_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def populate_and_verify_axis_id(cls, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        values = dict(raw)
        expected = _axis_identity(values)
        supplied = values.get("axis_id")
        if supplied is not None and supplied != expected:
            raise ValueError("axis_id does not match the normalized market coordinates")
        values["axis_id"] = expected
        return values


class ResolvedQuery(ContractModel):
    entities: list[EntityRef]
    axes: list[MarketAxisSpec] = Field(default_factory=list)
    capabilities: list[BrandCapabilitySnapshot] = Field(default_factory=list)
    resolution_status: ResolutionStatus
    parameter_status: ParameterStatus
    operation: str | None = None
    requested_metrics: list[str] = Field(default_factory=list)
    period: PeriodSpec | None = None
