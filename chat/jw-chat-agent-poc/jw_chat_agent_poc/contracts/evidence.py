from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from .base import ContractModel


class EvidenceStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class RationaleKind(StrEnum):
    CLASS_RECODE = "class_recode"
    CD_FILTER = "cd_filter"
    MARKET_MEMBERSHIP = "market_membership"


class EvidenceFact(ContractModel):
    fact_type: Literal["evidence"] = "evidence"
    evidence_id: str = Field(min_length=1)
    subject_type: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    subject_name: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    value: Decimal | str | None = None
    unit: str | None = None
    period_from: str | None = None
    period_to: str | None = None
    source: str = Field(min_length=1)
    view: str | None = None
    market_id: str | None = None
    axis_id: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    provenance: dict[str, JsonValue] = Field(default_factory=dict)
    status: EvidenceStatus

    @model_validator(mode="after")
    def require_calculation_lineage(self) -> Self:
        if self.provenance.get("type") != "deterministic_calculation":
            return self
        formula = self.provenance.get("formula")
        input_ids = self.provenance.get("input_evidence_ids")
        has_formula = isinstance(formula, str) and bool(formula.strip())
        has_inputs = (
            isinstance(input_ids, list)
            and bool(input_ids)
            and all(isinstance(item, str) and bool(item.strip()) for item in input_ids)
        )
        if not has_formula or not has_inputs:
            raise ValueError(
                "deterministic_calculation requires formula and input_evidence_ids"
            )
        return self


class RationaleFact(EvidenceFact):
    fact_type: Literal["rationale"] = "rationale"
    value: str | None = None
    rationale_kind: RationaleKind

    @model_validator(mode="after")
    def keep_missing_rationale_unclaimed(self) -> Self:
        has_content = isinstance(self.value, str) and bool(self.value.strip())
        match self.status:
            case EvidenceStatus.FOUND:
                if not has_content:
                    raise ValueError("FOUND rationale requires content")
            case EvidenceStatus.NOT_FOUND:
                if self.value is not None:
                    raise ValueError("NOT_FOUND rationale cannot contain claimed content")
            case EvidenceStatus.PARTIAL:
                if not has_content:
                    raise ValueError("PARTIAL rationale requires bounded content")
            case EvidenceStatus.UNAVAILABLE:
                if self.value is not None:
                    raise ValueError("UNAVAILABLE rationale cannot contain claimed content")
        return self


EvidenceItem = Annotated[
    EvidenceFact | RationaleFact,
    Field(discriminator="fact_type"),
]


class PartialFailure(ContractModel):
    reason_code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    requested_facets: tuple[str, ...] = ()
    unresolvable_facets: tuple[str, ...] = ()


class SourceStatus(ContractModel):
    source: str = Field(min_length=1)
    status: EvidenceStatus
    failure_kind: str | None = None


class EvidenceBundle(ContractModel):
    facts: tuple[EvidenceItem, ...] = ()
    notices: tuple[str, ...] = ()
    partial_failures: tuple[PartialFailure, ...] = ()
    source_statuses: tuple[SourceStatus, ...] = ()

    @property
    def bundle_hash(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
