from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Json = dict[str, Any] | list[Any] | str | int | float | bool | None
Approach = Literal["primitive", "query_spec"]


@dataclass(frozen=True, slots=True)
class StepTrace:
    """One tool-like action in a composition trace."""

    tool: str
    arguments: dict[str, Any]
    result_id: str | None = None
    rows: int | None = None
    summary: str = ""


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Deterministic facts returned by executing a selected analysis."""

    intent_id: str
    status: Literal["ok", "partial", "unsupported", "error"]
    facts: dict[str, Any]
    answer_md: str
    fact_keys: tuple[str, ...]
    notes: tuple[str, ...] = ()


@dataclass(slots=True)
class ApproachRun:
    """Full record for one question under one composition approach."""

    qid: str
    approach: Approach
    question: str
    expected_intent: str
    llm_raw: str = ""
    llm_json: dict[str, Any] = field(default_factory=dict)
    llm_grounded_json: dict[str, Any] = field(default_factory=dict)
    llm_parse_ok: bool = False
    llm_raw_schema_ok: bool = False
    llm_raw_schema_errors: list[str] = field(default_factory=list)
    llm_schema_ok: bool = False
    llm_schema_errors: list[str] = field(default_factory=list)
    grounding_changes: list[str] = field(default_factory=list)
    llm_intent: str = ""
    llm_error: str = ""
    analysis: AnalysisResult | None = None
    trace: list[StepTrace] = field(default_factory=list)
    elapsed_ms: float = 0.0

    @property
    def intent_ok(self) -> bool:
        return self.llm_intent == self.expected_intent

    @property
    def executable(self) -> bool:
        return self.llm_parse_ok and self.analysis is not None and self.analysis.status != "error"

    @property
    def fact_consistent(self) -> bool:
        return self.executable and self.analysis is not None and bool(self.analysis.fact_keys)
