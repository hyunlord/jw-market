from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, assert_never


EvidenceValue = str | int | float | None


class BQFusionMode(StrEnum):
    SIDE_BY_SIDE = "side_by_side"
    AGGREGATE = "aggregate"


class SourceKind(StrEnum):
    MARKET = "market"
    NEWS = "news"
    CSD = "csd"
    HIRA = "hira"
    FILE = "file"


class ContextField(StrEnum):
    PERIOD = "period"
    UNIT = "unit"
    VIEW = "view"
    MARKET_DEFINITION = "market_definition"
    SCOPE = "scope"


@dataclass(frozen=True, slots=True)
class BQFusionError(Exception):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class BQEvidenceSlice:
    kind: SourceKind
    source: str
    period: str | None
    unit: str | None
    view: str | None
    market_definition: str | None
    scope: str | None
    evidence_refs: tuple[str, ...]
    value: EvidenceValue = None

    @classmethod
    def market(
        cls,
        source: str,
        period: str,
        unit: str,
        view: str,
        market_definition: str,
        evidence_refs: tuple[str, ...],
        *,
        scope: str = "brand",
        value: EvidenceValue = None,
    ) -> BQEvidenceSlice:
        return cls(
            SourceKind.MARKET,
            source,
            period,
            unit,
            view,
            market_definition,
            scope,
            evidence_refs,
            value,
        )

    @classmethod
    def news(cls, source: str, evidence_refs: tuple[str, ...]) -> BQEvidenceSlice:
        return cls(SourceKind.NEWS, source, None, None, None, None, "event", evidence_refs)

    @classmethod
    def csd(cls, source: str, period: str, evidence_refs: tuple[str, ...]) -> BQEvidenceSlice:
        return cls(
            SourceKind.CSD,
            source,
            period,
            "건",
            "영업활동 aggregate",
            None,
            "product_activity",
            evidence_refs,
        )

    @classmethod
    def hira(cls, source: str, period: str, unit: str, evidence_refs: tuple[str, ...]) -> BQEvidenceSlice:
        return cls(
            SourceKind.HIRA,
            source,
            period,
            unit,
            "HIRA API",
            None,
            "external_stat",
            evidence_refs,
        )

    @classmethod
    def file(cls, source: str, evidence_refs: tuple[str, ...]) -> BQEvidenceSlice:
        return cls(SourceKind.FILE, source, None, None, "파일", None, "file", evidence_refs)


@dataclass(frozen=True, slots=True)
class BQFusionRequest:
    mode: BQFusionMode
    slices: tuple[BQEvidenceSlice, ...]


@dataclass(frozen=True, slots=True)
class BQFusionPlan:
    mode: BQFusionMode
    slices: tuple[BQEvidenceSlice, ...]
    can_aggregate: bool

    @property
    def has_source_divergence(self) -> bool:
        return len({_source_family(item.source) for item in self.slices}) > 1


_AGGREGATION_CONTEXT_FIELDS: Final[tuple[ContextField, ...]] = (
    ContextField.PERIOD,
    ContextField.UNIT,
    ContextField.VIEW,
    ContextField.MARKET_DEFINITION,
    ContextField.SCOPE,
)


def validate_fusion_request(request: BQFusionRequest) -> BQFusionPlan:
    if not request.slices:
        raise BQFusionError("fusion request must include at least one evidence slice")
    match request.mode:
        case BQFusionMode.SIDE_BY_SIDE:
            return BQFusionPlan(request.mode, request.slices, can_aggregate=False)
        case BQFusionMode.AGGREGATE:
            _validate_aggregation(request.slices)
            return BQFusionPlan(request.mode, request.slices, can_aggregate=True)
        case unreachable:
            assert_never(unreachable)


def _validate_aggregation(slices: tuple[BQEvidenceSlice, ...]) -> None:
    kinds = {item.kind for item in slices}
    if SourceKind.FILE in kinds and SourceKind.MARKET in kinds:
        raise BQFusionError("cannot aggregate FILE+MARKET evidence; render side-by-side")
    source_families = {_source_family(item.source) for item in slices}
    if {"ubist", "iqvia"}.issubset(source_families):
        raise BQFusionError("cannot aggregate divergent UBIST+IQVIA market sources")
    if len(kinds) > 1:
        raise BQFusionError("cannot aggregate divergent source kinds; render side-by-side")
    for field_name in _AGGREGATION_CONTEXT_FIELDS:
        _validate_same_context(slices, field_name)


def _validate_same_context(
    slices: tuple[BQEvidenceSlice, ...],
    field_name: ContextField,
) -> None:
    values = {
        _context_value(item, field_name)
        for item in slices
        if _context_value(item, field_name) is not None
    }
    if len(values) > 1:
        raise BQFusionError(f"incompatible {field_name} for aggregation")


def _context_value(item: BQEvidenceSlice, field_name: ContextField) -> str | None:
    match field_name:
        case ContextField.PERIOD:
            return item.period
        case ContextField.UNIT:
            return item.unit
        case ContextField.VIEW:
            return item.view
        case ContextField.MARKET_DEFINITION:
            return item.market_definition
        case ContextField.SCOPE:
            return item.scope
        case unreachable:
            assert_never(unreachable)


def _source_family(source: str) -> str:
    normalized = source.casefold().replace(" ", "_")
    if normalized.startswith("iqvia"):
        return "iqvia"
    if normalized.startswith("ubist"):
        return "ubist"
    if "event_brand_scores" in normalized or "deep_analysis" in normalized:
        return "news"
    if normalized.startswith("csd"):
        return "csd"
    if normalized.startswith("hira"):
        return "hira"
    if "file" in normalized or "업로드" in normalized:
        return "file"
    return normalized
