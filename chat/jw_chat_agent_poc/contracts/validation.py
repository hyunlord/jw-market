from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .base import ContractModel


class ValidationSeverity(StrEnum):
    WARNING = "warning"
    ERROR = "error"


class ValidationDecision(StrEnum):
    ALLOW = "allow"
    REPAIR = "repair"
    REJECT = "reject"


class ValidationViolation(ContractModel):
    rule_id: str = Field(min_length=1)
    severity: ValidationSeverity
    location: str = Field(min_length=1)
    message: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = ()


class ValidationReport(ContractModel):
    decision: ValidationDecision
    violations: tuple[ValidationViolation, ...] = ()


class RenderAuthorization(ContractModel):
    passed: bool
    authorized_chart_ids: tuple[str, ...] = ()
    evidence_bundle_hash: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
