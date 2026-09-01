from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact
from jw_chat_agent_poc.service.evidence_binding_rules import (
    entity_matches,
    metric_matches,
    period_matches,
    scope_matches,
    token_unit,
    unit_matches,
)

_MAX_REJECTION_CANDIDATES: Final = 8


@dataclass(frozen=True, slots=True)
class RejectionExpectation:
    entity: tuple[str, ...]
    metric: tuple[str, ...]
    period: tuple[str, ...]
    unit: str
    view: tuple[str, ...]
    market_id: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RejectionCandidate:
    entity: str
    metric: str
    period: str
    unit: str
    view: str
    market_id: str
    mismatched_axes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClaimRejectionDiagnostic:
    token: str
    reason: str
    expected: RejectionExpectation
    candidates: tuple[RejectionCandidate, ...]
    candidate_count: int
    candidates_truncated: bool

    def to_trace(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "reason": self.reason,
            "expected": {
                "entity": self.expected.entity,
                "metric": self.expected.metric,
                "period": self.expected.period,
                "unit": self.expected.unit,
                "view": self.expected.view,
                "market_id": self.expected.market_id,
            },
            "candidates": tuple(
                {
                    "entity": candidate.entity,
                    "metric": candidate.metric,
                    "period": candidate.period,
                    "unit": candidate.unit,
                    "view": candidate.view,
                    "market_id": candidate.market_id,
                    "mismatched_axes": candidate.mismatched_axes,
                }
                for candidate in self.candidates
            ),
            "candidate_count": self.candidate_count,
            "candidates_truncated": self.candidates_truncated,
        }


def rejection_diagnostic(
    *,
    token: str,
    reason: str,
    candidates: Sequence[EvidenceFact],
    expected_entities: set[str],
    expected_metrics: tuple[str, ...],
    requested_periods: tuple[str, ...],
    expected_scopes: frozenset[str],
    expected_market_ids: frozenset[str],
    forced_mismatch_axes: tuple[str, ...] = (),
) -> ClaimRejectionDiagnostic:
    expected = RejectionExpectation(
        entity=tuple(sorted(expected_entities)),
        metric=expected_metrics,
        period=requested_periods,
        unit=token_unit(token),
        view=tuple(sorted(expected_scopes)),
        market_id=tuple(sorted(expected_market_ids)),
    )
    projected = tuple(
        _rejection_candidate(
            fact,
            token=token,
            expected_entities=expected_entities,
            expected_metrics=expected_metrics,
            requested_periods=requested_periods,
            expected_scopes=expected_scopes,
            expected_market_ids=expected_market_ids,
            forced_mismatch_axes=forced_mismatch_axes,
        )
        for fact in candidates[:_MAX_REJECTION_CANDIDATES]
    )
    return ClaimRejectionDiagnostic(
        token=token,
        reason=reason,
        expected=expected,
        candidates=projected,
        candidate_count=len(candidates),
        candidates_truncated=len(candidates) > _MAX_REJECTION_CANDIDATES,
    )


def _rejection_candidate(
    fact: EvidenceFact,
    *,
    token: str,
    expected_entities: set[str],
    expected_metrics: tuple[str, ...],
    requested_periods: tuple[str, ...],
    expected_scopes: frozenset[str],
    expected_market_ids: frozenset[str],
    forced_mismatch_axes: tuple[str, ...],
) -> RejectionCandidate:
    mismatched_axes = [
        axis
        for axis, matches in (
            ("entity", entity_matches(fact, expected_entities)),
            ("metric", metric_matches(fact, expected_metrics)),
            ("period", period_matches(fact, requested_periods)),
            ("unit", unit_matches(fact, token)),
            ("view", scope_matches(fact, expected_scopes)),
            (
                "market_id",
                scope_matches(fact, frozenset(), expected_market_ids),
            ),
        )
        if not matches
    ]
    mismatched_axes.extend(
        axis for axis in forced_mismatch_axes if axis not in mismatched_axes
    )
    return RejectionCandidate(
        entity=fact.entity,
        metric=fact.metric,
        period=fact.period,
        unit=fact.unit,
        view=fact.view,
        market_id=fact.market_id,
        mismatched_axes=tuple(mismatched_axes),
    )
