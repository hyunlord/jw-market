from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, TypeAlias


@dataclass(frozen=True, slots=True)
class ExecutableTool:
    name: str
    domain: str
    timeout_s: float
    execute: Callable[[dict[str, object]], object]


@dataclass(frozen=True, slots=True)
class ToolExecutionRecord:
    tool_name: str
    arguments: dict[str, object]
    raw_result: object
    latency_ms: float


@dataclass(frozen=True, slots=True)
class ToolFailureRecord:
    tool_name: str
    arguments: dict[str, object]
    stage: str
    error_type: str
    message: str
    latency_ms: float | None = None


@dataclass(frozen=True, slots=True)
class ToolDeferredRecord:
    tool_name: str
    arguments: dict[str, object]
    stage: str
    reason: str
    latency_ms: float | None = None


@dataclass(frozen=True, slots=True)
class MarketMetricFact:
    fact_type: ClassVar[str] = "market_metric"
    evidence_id: str
    tool_name: str
    arguments: dict[str, object]
    raw_result: object
    missing_required_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegulatoryRuleFact:
    fact_type: ClassVar[str] = "regulatory_rule"
    evidence_id: str
    tool_name: str
    arguments: dict[str, object]
    raw_result: object
    missing_required_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClinicalTrialFact:
    fact_type: ClassVar[str] = "clinical_trial"
    evidence_id: str
    tool_name: str
    arguments: dict[str, object]
    raw_result: object
    missing_required_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FileCellFact:
    fact_type: ClassVar[str] = "file_cell"
    evidence_id: str
    tool_name: str
    arguments: dict[str, object]
    raw_result: object
    missing_required_fields: tuple[str, ...]


V3EvidenceFact: TypeAlias = (
    MarketMetricFact | RegulatoryRuleFact | ClinicalTrialFact | FileCellFact
)


@dataclass(frozen=True, slots=True)
class V3EvidenceBundle:
    status: str
    facts: tuple[V3EvidenceFact, ...]
    failures: tuple[ToolFailureRecord, ...]
    deferred: tuple[ToolDeferredRecord, ...]
    executions: tuple[ToolExecutionRecord, ...]
    original_call_count: int
    executed_call_count: int
    deduplicated_call_count: int

    def summary(self) -> dict[str, object]:
        return {
            "status": self.status,
            "original_call_count": self.original_call_count,
            "executed_call_count": self.executed_call_count,
            "deduplicated_call_count": self.deduplicated_call_count,
            "fact_count": len(self.facts),
            "failure_count": len(self.failures),
            "deferred_count": len(self.deferred),
            "fact_types": [fact.fact_type for fact in self.facts],
            "consumed_by_serving_path": False,
            "answer_generation_count": 0,
        }
