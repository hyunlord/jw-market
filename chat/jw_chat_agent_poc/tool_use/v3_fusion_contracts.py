from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _FusionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GeneratedFusionClaim(_FusionContract):
    text: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = ()


class GeneratedFusionAnswer(_FusionContract):
    claims: tuple[GeneratedFusionClaim, ...] = ()
    limitations: tuple[str, ...] = ()


class FusionClaim(_FusionContract):
    text: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)


class FusionAnswerModel(_FusionContract):
    claims: tuple[FusionClaim, ...] = ()
    limitations: tuple[str, ...] = ()


class RejectedFusionClaim(_FusionContract):
    text: str
    evidence_ids: tuple[str, ...]
    reason: str
    numeric_literals: tuple[str, ...] = ()


class FusionAudit(_FusionContract):
    rejected_claims: tuple[RejectedFusionClaim, ...] = ()
    ungrounded_numeric_literals: tuple[str, ...] = ()
    rejected_limitations: tuple[str, ...] = ()
    injected_limitation_reason_codes: tuple[str, ...] = ()


class ValidatedFusionAnswer(_FusionContract):
    answer: FusionAnswerModel
    audit: FusionAudit
