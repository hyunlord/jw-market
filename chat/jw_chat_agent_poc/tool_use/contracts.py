from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class FallbackCode(StrEnum):
    STEP_LIMIT = "STEP_LIMIT"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    VERIFICATION_FAIL = "VERIFICATION_FAIL"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    UNSUPPORTED_QUERY = "UNSUPPORTED_QUERY"


class EvidenceFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str
    subject: str
    metric: str
    value: Decimal | None
    unit: str | None
    period: str | None
    source_name: str
    source_locator: str | None
    raw_ref: str | None


class ToolEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    preview: str
    evidence: tuple[EvidenceFact, ...]
    missing_requested_facets: tuple[str, ...] = ()
    raw: dict | list | None
    error_code: str | None
    error_message: str | None


class ToolTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step: int
    tool: str | None
    status: str
    fallback_code: FallbackCode | None
    message: str


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str
    answer: str
    tool_calls: tuple[dict, ...]
    sources: tuple[str, ...]
    traces: tuple[ToolTrace, ...]
    fallback_code: FallbackCode | None
