from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .base import ContractModel


class RouteMode(StrEnum):
    DETERMINISTIC = "deterministic"
    WORKFLOW = "workflow"
    AGENTIC = "agentic"


class RejectedRoute(ContractModel):
    domain: str = Field(min_length=1)
    handler: str = Field(min_length=1)
    reason_codes: tuple[str, ...] = Field(min_length=1)


class RouteDecision(ContractModel):
    domain: str = Field(min_length=1)
    handler: str = Field(min_length=1)
    mode: RouteMode
    decided_by: str = Field(min_length=1)
    reason_codes: tuple[str, ...] = Field(min_length=1)
    rejected_alternatives: tuple[RejectedRoute, ...] = ()
    clarification_message: str | None = None
