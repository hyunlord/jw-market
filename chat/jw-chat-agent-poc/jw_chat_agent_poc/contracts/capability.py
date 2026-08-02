from __future__ import annotations

from typing import Final, assert_never

from .query import (
    BrandCapabilitySnapshot,
    CatalogSourceClass,
    EntityRef,
    MarketSource,
    NativeMarketMeasure,
    ParameterIssueReason,
    ParameterResolution,
    ParameterStatus,
    ResolutionStatus,
    SourceMeasureCapability,
)


_SOURCE_MEASURES: Final[dict[MarketSource, tuple[NativeMarketMeasure, ...]]] = {
    MarketSource.UBIST: (
        NativeMarketMeasure.SALES,
        NativeMarketMeasure.VOLUME,
    ),
    MarketSource.IQVIA: (
        NativeMarketMeasure.SALES,
        NativeMarketMeasure.UNIT,
        NativeMarketMeasure.DOSAGE_UNIT,
        NativeMarketMeasure.COUNTING_UNIT,
    ),
}


def _sources_for_class(source_class: CatalogSourceClass) -> tuple[MarketSource, ...]:
    match source_class:
        case CatalogSourceClass.UBIST_ONLY:
            return (MarketSource.UBIST,)
        case CatalogSourceClass.IQVIA_ONLY:
            return (MarketSource.IQVIA,)
        case CatalogSourceClass.DUAL:
            return (MarketSource.UBIST, MarketSource.IQVIA)
        case unreachable:
            assert_never(unreachable)


def capability_snapshot_from_source_class(
    *,
    entity: EntityRef,
    source_class: CatalogSourceClass,
    catalog_snapshot_id: str,
) -> BrandCapabilitySnapshot:
    """Translate catalog-owned source_class without brand-specific rules."""

    return BrandCapabilitySnapshot(
        entity=entity,
        source_class=source_class,
        source_capabilities=tuple(
            SourceMeasureCapability(
                source=source,
                native_measures=_SOURCE_MEASURES[source],
            )
            for source in _sources_for_class(source_class)
        ),
        catalog_snapshot_id=catalog_snapshot_id,
    )


def parameter_resolution(
    capability: BrandCapabilitySnapshot,
    source: MarketSource,
    native_measure: NativeMarketMeasure,
) -> ParameterResolution:
    source_capability = next(
        (
            candidate
            for candidate in capability.source_capabilities
            if candidate.source is source
        ),
        None,
    )
    if source_capability is None:
        return ParameterResolution(
            resolution_status=ResolutionStatus.RESOLVED,
            status=ParameterStatus.UNSUPPORTED_COMBINATION,
            reason=ParameterIssueReason.BRAND_NOT_IN_SOURCE,
            requested_source=source,
            requested_native_measure=native_measure,
        )
    if native_measure not in source_capability.native_measures:
        return ParameterResolution(
            resolution_status=ResolutionStatus.RESOLVED,
            status=ParameterStatus.UNSUPPORTED_COMBINATION,
            reason=ParameterIssueReason.INVALID_MEASURE_FOR_SOURCE,
            requested_source=source,
            requested_native_measure=native_measure,
            valid_measures=source_capability.native_measures,
        )
    return ParameterResolution(
        resolution_status=ResolutionStatus.RESOLVED,
        status=ParameterStatus.VALID,
        requested_source=source,
        requested_native_measure=native_measure,
        valid_measures=source_capability.native_measures,
    )
