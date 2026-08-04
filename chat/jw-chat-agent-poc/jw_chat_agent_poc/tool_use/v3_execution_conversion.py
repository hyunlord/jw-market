from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib

from jw_chat_agent_poc.tool_use.contracts import ToolEnvelope
from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    ClinicalTrialFact,
    FileCellFact,
    MarketMetricFact,
    RegulatoryRuleFact,
    ToolExecutionRecord,
    ToolDeferredRecord,
    ToolFailureRecord,
    V3EvidenceFact,
)
from jw_chat_agent_poc.tool_use.v3_execution_normalization import (
    canonical_argument_key,
)


def convert_execution(
    record: ToolExecutionRecord,
    domain: str,
) -> tuple[
    V3EvidenceFact | None,
    ToolFailureRecord | None,
    ToolDeferredRecord | None,
]:
    if record.tool_name == "web_search":
        return None, None, ToolDeferredRecord(
            record.tool_name,
            record.arguments,
            "conversion",
            "WebSourceFact is intentionally deferred to Phase C-3",
            record.latency_ms,
        )

    evidence_id = _evidence_id(record.tool_name, record.arguments)
    if domain == "market":
        return MarketMetricFact(
            evidence_id,
            record.tool_name,
            record.arguments,
            record.raw_result,
            _missing_fields(
                record,
                ("brand", "metric", "period", "unit", "view", "market"),
            ),
        ), None, None
    if domain == "clinical":
        return ClinicalTrialFact(
            evidence_id,
            record.tool_name,
            record.arguments,
            record.raw_result,
            _missing_fields(record, ("status", "last_update_posted")),
        ), None, None
    if domain == "file":
        return FileCellFact(
            evidence_id,
            record.tool_name,
            record.arguments,
            record.raw_result,
            _missing_fields(record, ("file_id", "sheet", "range")),
        ), None, None
    return RegulatoryRuleFact(
        evidence_id,
        record.tool_name,
        record.arguments,
        record.raw_result,
        _missing_fields(record, ("effective_date", "last_checked")),
    ), None, None


def tool_domain(name: str) -> str:
    if name.startswith("market."):
        return "market"
    if name.startswith("file."):
        return "file"
    if name.startswith("clinicaltrials_") or name == "mfds_clinical_trial_kr":
        return "clinical"
    if name == "web_search":
        return "general"
    return "regulatory"


def bundle_status(
    executions: Sequence[ToolExecutionRecord],
    failures: Sequence[ToolFailureRecord],
) -> str:
    if executions and failures:
        return "partial"
    if executions:
        return "complete"
    return "failed"


def failure_sort_key(record: ToolFailureRecord) -> tuple[str, str, str]:
    return record.tool_name, record.stage, record.error_type


def _missing_fields(
    record: ToolExecutionRecord,
    required: tuple[str, ...],
) -> tuple[str, ...]:
    available = set(record.arguments)
    raw = (
        record.raw_result.raw
        if isinstance(record.raw_result, ToolEnvelope)
        else record.raw_result
    )
    if isinstance(raw, Mapping):
        available.update(str(key) for key in raw)
        render_data = raw.get("render_data")
        if isinstance(render_data, Mapping):
            available.update(str(key) for key in render_data)
    return tuple(field for field in required if field not in available)


def _evidence_id(tool_name: str, arguments: Mapping[str, object]) -> str:
    normalized = repr(canonical_argument_key(tool_name, arguments)).encode()
    digest = hashlib.sha256(normalized).hexdigest()
    return f"v3-shadow:{tool_name}:{digest[:16]}"
