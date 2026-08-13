from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


RenderProfile = Literal[
    "market_analysis",
    "clinical_portfolio",
    "patent_portfolio",
    "policy_document",
    "single_record_detail",
]


class LosslessInvariantError(ValueError):
    """Raised when deterministic rendering violates a coverage invariant."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceReference(_StrictModel):
    url: str
    title: str | None = None
    published_at: str | None = None


class EvidenceRecord(_StrictModel):
    evidence_id: str
    source: str
    result_kind: str
    payload: dict[str, Any]
    source_refs: tuple[SourceReference, ...] = ()


class CoverageLedger(_StrictModel):
    total_reported: int | None = None
    records_after_status_filter: int | None = None
    records_received: int = 0
    records_unique: int = 0
    records_relevant: int | None = None
    records_excluded_by_status: int | None = None
    records_excluded_by_relevance: int = 0
    records_rendered: int = 0
    pagination_complete: bool = True
    partial_reasons: tuple[str, ...] = ()


class EvidenceSet(_StrictModel):
    source: str
    query_spec: tuple[str, ...] = ()
    query_manifest: tuple[dict[str, Any], ...] = ()
    retrieved_at: str
    coverage: CoverageLedger
    records: tuple[EvidenceRecord, ...] = ()
    item_failures: tuple[dict[str, Any], ...] = ()
    source_refs: tuple[SourceReference, ...] = ()


class RenderNode(_StrictModel):
    block_id: str
    record_ids: tuple[str, ...] = ()
    surface_fields: tuple[str, ...] = ()
    text: str


class DeterministicRender(_StrictModel):
    profile: RenderProfile
    text: str = ""
    nodes: tuple[RenderNode, ...] = ()
    coverage: CoverageLedger = Field(default_factory=CoverageLedger)
    source_refs: tuple[SourceReference, ...] = ()
    required_fields: tuple[str, ...] = ()
    record_surface_rate: float = 1.0
    required_field_surface_rate: float = 1.0
    request_notice: str | None = None
    source_notices: tuple[str, ...] = ()
    source_notice_bindings: tuple[dict[str, Any], ...] = ()
    source_tiers: dict[str, int] = Field(default_factory=dict)


class CompositionResult(_StrictModel):
    text: str
    answer_mutated: bool
    fallback_detail_retention_rate: float
    trace: dict[str, Any] = Field(default_factory=dict)
