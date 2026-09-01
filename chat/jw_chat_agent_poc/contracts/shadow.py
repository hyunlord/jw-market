from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypedDict

from .evidence import EvidenceBundle, EvidenceFact, EvidenceStatus

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


class LegacyEvidenceFactLike(Protocol):
    fact_id: str
    label: str
    value: str
    source: str
    tool: str
    path: str
    period: str
    entity: str
    metric: str
    unit: str
    view: str
    market_id: str


class EvidenceBundleShadowObservation(TypedDict):
    contract: str
    evidence_bundle: dict


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


def evidence_bundle_from_legacy_facts(
    facts: Sequence[LegacyEvidenceFactLike],
) -> EvidenceBundle:
    """Project existing facts without supplying unavailable market coordinates."""

    projected = tuple(
        EvidenceFact(
            evidence_id=fact.fact_id,
            subject_type="entity" if fact.entity else "fact",
            subject_id=fact.entity or fact.fact_id,
            subject_name=fact.entity or fact.label,
            metric=fact.metric or fact.label,
            value=fact.value or None,
            unit=fact.unit or None,
            period_from=fact.period or None,
            period_to=fact.period or None,
            source=fact.source,
            view=fact.view or None,
            market_id=fact.market_id or None,
            axis_id=None,
            provenance={
                "type": "legacy_projection",
                "legacy_fact_id": fact.fact_id,
                "tool": fact.tool,
                "path": fact.path,
            },
            status=EvidenceStatus.FOUND if fact.value else EvidenceStatus.NOT_FOUND,
        )
        for fact in facts
    )
    return EvidenceBundle(facts=projected)


def evidence_bundle_shadow_observation(
    facts: Sequence[LegacyEvidenceFactLike],
) -> EvidenceBundleShadowObservation:
    bundle = evidence_bundle_from_legacy_facts(facts)
    return {
        "contract": "evidence_bundle_phase1b_shadow_v1",
        "evidence_bundle": bundle.model_dump(mode="json"),
    }
