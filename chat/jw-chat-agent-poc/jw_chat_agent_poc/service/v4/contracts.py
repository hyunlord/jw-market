from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SourceName = Literal[
    "mart",
    "nedrug",
    "hira",
    "openfda",
    "clinicaltrials",
    "web",
    "patent",
]
SOURCE_NAMES: tuple[SourceName, ...] = (
    "mart",
    "nedrug",
    "hira",
    "openfda",
    "clinicaltrials",
    "web",
    "patent",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolQueries(_StrictModel):
    mart: tuple[str, ...] = Field(min_length=1)
    nedrug: tuple[str, ...] = Field(min_length=1)
    hira: tuple[str, ...] = Field(min_length=1)
    openfda: tuple[str, ...] = Field(min_length=1)
    clinicaltrials: tuple[str, ...] = Field(min_length=1)
    web: tuple[str, ...] = Field(min_length=1)
    patent: tuple[str, ...] = Field(min_length=1)

    @field_validator("mart", "nedrug", "hira", "openfda", "clinicaltrials", "web", "patent")
    @classmethod
    def queries_must_have_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(" ".join(value.split()) for value in values if value.strip())
        if not normalized:
            raise ValueError("every source requires at least one non-empty query")
        return normalized

    def items(self) -> tuple[tuple[SourceName, tuple[str, ...]], ...]:
        return tuple((name, getattr(self, name)) for name in SOURCE_NAMES)


class PlannerOutput(_StrictModel):
    resolved_question: str = Field(min_length=1)
    expanded_intents: tuple[str, ...] = Field(min_length=1)
    tool_queries: ToolQueries
    linking_plan: str = Field(min_length=1)
    needs_second_hop: bool = False


class Citation(_StrictModel):
    source: str = Field(min_length=1)
    query: str = Field(min_length=1)
    url: str | None = None
    retrieved_at: datetime
    used: bool = False


class SourceResult(_StrictModel):
    source: SourceName
    query: str
    status: Literal["ok", "empty", "error", "timeout"]
    payload: Any = None
    citations: tuple[Citation, ...] = ()
    elapsed_ms: float = 0.0
    notice: str | None = None
    cache_hit: bool = False


class GatedAnswer(_StrictModel):
    text: str
    trace: dict[str, Any] = Field(default_factory=dict)


class V4Answer(_StrictModel):
    text: str
    charts: tuple[dict[str, Any], ...] = ()
    sources: tuple[str, ...] = ()
    trace: dict[str, Any] = Field(default_factory=dict)
    timing: dict[str, Any] = Field(default_factory=dict)
    conversation_id: str | None = None
