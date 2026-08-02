from __future__ import annotations

from typing import Protocol, TypedDict

from .query import (
    EntityKind,
    EntityRef,
    ParameterStatus,
    PeriodGranularity,
    PeriodSpec,
    ResolutionStatus,
    ResolvedQuery,
)


class QuerySpecEntity(Protocol):
    kind: QuerySpecValue
    canonical_id: str
    display_name: str


class QuerySpecValue(Protocol):
    value: str


class QuerySpecLike(Protocol):
    entities: tuple[QuerySpecEntity, ...]
    operation: QuerySpecValue
    metrics: tuple[str, ...]
    start_period: str | None
    end_period: str | None
    granularity: QuerySpecValue | None


class ResolvedQueryShadowObservation(TypedDict):
    contract: str
    resolved_query: dict


def resolved_query_from_query_spec(spec: QuerySpecLike) -> ResolvedQuery:
    """Create an unconsumed Phase 1-A shadow contract from the legacy spec."""

    entities = [
        EntityRef(
            kind=EntityKind(entity.kind.value),
            canonical_id=entity.canonical_id,
            display_name=entity.display_name,
        )
        for entity in spec.entities
    ]
    period = None
    if spec.start_period is not None or spec.end_period is not None:
        period = PeriodSpec(
            start=spec.start_period,
            end=spec.end_period,
            granularity=(
                PeriodGranularity(spec.granularity.value)
                if spec.granularity is not None
                else None
            ),
        )
    return ResolvedQuery(
        entities=entities,
        resolution_status=(
            ResolutionStatus.RESOLVED if entities else ResolutionStatus.UNRESOLVED
        ),
        parameter_status=ParameterStatus.NOT_APPLICABLE,
        operation=spec.operation.value,
        requested_metrics=list(spec.metrics),
        period=period,
    )


def resolved_query_shadow_observation(
    spec: QuerySpecLike,
) -> ResolvedQueryShadowObservation:
    resolved = resolved_query_from_query_spec(spec)
    return {
        "contract": "resolved_query_phase1a_shadow_v1",
        "resolved_query": resolved.model_dump(mode="json"),
    }
