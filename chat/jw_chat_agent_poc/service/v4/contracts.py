from __future__ import annotations

from datetime import datetime
from enum import StrEnum
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
    "document",
    "prior_turn",
]
AbsenceStatus = Literal[
    "doc_not_found",
    "coverage_unknown",
    "confirmed_non_reimbursed",
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


def tool_query_sources(sources: tuple[SourceName, ...]) -> tuple[SourceName, ...]:
    """Return sources backed by ``ToolQueries`` executor fields."""

    return tuple(source for source in sources if source in SOURCE_NAMES)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolQueries(_StrictModel):
    mart: tuple[str, ...] = Field(min_length=1)
    nedrug: tuple[str, ...] = Field(min_length=1)
    hira: tuple[str, ...] = Field(min_length=1)
    openfda: tuple[str, ...] = Field(min_length=1)
    clinicaltrials: tuple[str, ...] = Field(min_length=1, max_length=32)
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


class ClinicalTrialConcept(_StrictModel):
    ingredients: tuple[str, ...] = ()
    brands: tuple[str, ...] = ()
    search_area: Literal["intervention", "condition"] = "intervention"
    match: Literal["both", "any"] = "any"
    countries: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    source_queries: tuple[str, ...] = ()
    expansion_source: Literal[
        "none",
        "mart_molecule",
        "nedrug_ingredient",
        "entity_variant_dictionary",
    ] = "none"
    expansion_status: Literal["not_requested", "resolved", "empty", "failed"] = (
        "not_requested"
    )
    expansion_grade: Literal[
        "notation_variant", "composition_component", "derived_product"
    ] = "notation_variant"


class RequestedAnswerShape(_StrictModel):
    entities: tuple[str, ...] = ()
    measure_or_attribute: tuple[str, ...] = ()
    time_horizon: str | None = None
    granularity: str | None = None
    period_from: str | None = None
    period_to: str | None = None
    period_count: int | None = Field(default=None, ge=1, le=120)


class AnswerShape(StrEnum):
    SCALAR_LOOKUP = "SCALAR_LOOKUP"
    MULTI_FIELD_LOOKUP = "MULTI_FIELD_LOOKUP"
    TIME_SERIES = "TIME_SERIES"
    COMPARISON = "COMPARISON"
    RANKING = "RANKING"
    GROUP_DISTRIBUTION = "GROUP_DISTRIBUTION"
    DOCUMENT_LIST_EXTRACT = "DOCUMENT_LIST_EXTRACT"
    DOCUMENT_SUMMARY = "DOCUMENT_SUMMARY"
    POLICY_TEXT = "POLICY_TEXT"


class RequiredAnswerItem(_StrictModel):
    id: str = Field(min_length=1)
    ask: str = Field(min_length=1)
    kind: Literal["data", "reading"]

    @field_validator("id", "ask")
    @classmethod
    def text_must_be_normalized(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("answer item text must not be empty")
        return normalized


class AnswerContract(_StrictModel):
    question_core: str = ""
    required_items: tuple[RequiredAnswerItem, ...] = Field(default=(), max_length=16)
    resolved_entities: tuple[str, ...] = ()
    required_items_degraded: bool = True
    required_entities: tuple[str, ...] = ()
    required_metrics: tuple[str, ...] = ()
    required_periods: tuple[str, ...] = ()
    required_dimensions: tuple[str, ...] = ()
    answer_shape: AnswerShape = AnswerShape.SCALAR_LOOKUP
    required_sources: tuple[SourceName, ...] = ()
    denominator_policy: Literal[
        "not_applicable",
        "same_scope_total",
        "same_grain_only",
    ] = "not_applicable"
    forbidden_substitutions: tuple[str, ...] = ()
    top_k: int | None = Field(default=None, ge=1, le=100)
    required_period_count: int | None = Field(default=None, ge=1, le=120)


class QueryScope(_StrictModel):
    requested_calls: dict[SourceName, int] = Field(default_factory=dict)
    executed_calls: dict[SourceName, int] = Field(default_factory=dict)
    omitted_queries: dict[SourceName, tuple[str, ...]] = Field(default_factory=dict)
    unexecuted_reasons: dict[SourceName, str] = Field(default_factory=dict)


class PlannerOutput(_StrictModel):
    resolved_question: str = Field(min_length=1)
    expanded_intents: tuple[str, ...] = Field(min_length=1)
    answer_sources: tuple[SourceName, ...] = SOURCE_NAMES
    tool_queries: ToolQueries
    linking_plan: str = Field(min_length=1)
    needs_second_hop: bool = False
    clinical_query_specs: tuple[ClinicalTrialConcept, ...] = Field(
        default=(),
        max_length=32,
    )
    requested_answer_shape: RequestedAnswerShape = Field(
        default_factory=RequestedAnswerShape
    )
    answer_contract: AnswerContract = Field(default_factory=AnswerContract)
    query_scope: QueryScope | None = None

    @field_validator("answer_sources")
    @classmethod
    def answer_sources_must_not_be_empty(
        cls,
        values: tuple[SourceName, ...],
    ) -> tuple[SourceName, ...]:
        if not values:
            raise ValueError("at least one answer source is required")
        return tuple(dict.fromkeys(values))


class Citation(_StrictModel):
    source: str = Field(min_length=1)
    query: str = Field(min_length=1)
    url: str | None = None
    retrieved_at: datetime
    used: bool = False


class EvidenceEnvelope(_StrictModel):
    kind: Literal["mart", "hira", "clinical", "nedrug", "openfda", "web", "patent"]
    entity_match: Literal["EXACT", "PARTIAL", "MISMATCH"]
    source_scope: Literal["KR", "US", "GLOBAL"]
    time_match: Literal["MATCH", "MISMATCH", "NOT_REQUESTED"]
    eligible_claims: tuple[str, ...] = ()
    causal: bool | None = None
    metric_type: str | None = None
    period: tuple[str, ...] = ()
    unit: dict[str, str] = Field(default_factory=dict)
    study_type: str | None = None
    intervention_type: tuple[str, ...] = ()
    phase: tuple[str, ...] = ()
    recruitment_status: str | None = None
    country: tuple[str, ...] = ()
    disease: tuple[str, ...] = ()
    product: tuple[str, ...] = ()
    ingredient: tuple[str, ...] = ()
    company: tuple[str, ...] = ()
    approval_date: tuple[str, ...] = ()
    subject_grain: Literal[
        "market",
        "ingredient",
        "company",
        "brand",
        "channel",
        "specialty",
        "unknown",
    ] = "unknown"
    period_start: str | None = None
    period_end: str | None = None
    parent_entity: str | None = None
    eligible_attributions: tuple[str, ...] = ()


class AbsenceConfirmation(_StrictModel):
    source: Literal["hira", "nedrug"]
    doc_type: Literal["reimbursement", "approval"]
    status: AbsenceStatus
    subject: str = Field(min_length=1)


class SourceResult(_StrictModel):
    source: SourceName
    query: str
    status: Literal[
        "ok",
        "empty",
        "error",
        "timeout",
        "quota",
        "upstream",
        "parse_error",
        "deadline_exceeded",
        "scope_limit",
        "no_document",
    ]
    payload: Any = None
    citations: tuple[Citation, ...] = ()
    elapsed_ms: float = 0.0
    notice: str | None = None
    cache_hit: bool = False
    evidence: EvidenceEnvelope | None = None
    failure_reason: str | None = None
    failure_detail: dict[str, Any] = Field(default_factory=dict)


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
