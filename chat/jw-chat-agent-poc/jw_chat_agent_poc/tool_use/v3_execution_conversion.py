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
from jw_chat_agent_poc.tool_use.v3_fact_projection import (
    project_clinical_fact,
    project_file_fact,
    project_market_fact,
    project_regulatory_fact,
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
    raw = _raw_payload(record.raw_result)
    if domain == "market":
        projection = project_market_fact(record.tool_name, record.arguments, raw)
        return MarketMetricFact(
            evidence_id=evidence_id,
            tool_name=record.tool_name,
            arguments=record.arguments,
            raw_result=record.raw_result,
            missing_required_fields=projection.missing(
                ("entity", "metric", "period", "unit", "view", "market")
            ),
            entity=projection.values["entity"],
            metric=projection.values["metric"],
            period=projection.values["period"],
            unit=projection.values["unit"],
            view=projection.values["view"],
            market=projection.values["market"],
            projection_sources=projection.sources,
            projection_missing_reasons=projection.reasons(
                ("entity", "metric", "period", "unit", "view", "market")
            ),
        ), None, None
    if domain == "clinical":
        projection = project_clinical_fact(raw)
        return ClinicalTrialFact(
            evidence_id=evidence_id,
            tool_name=record.tool_name,
            arguments=record.arguments,
            raw_result=record.raw_result,
            missing_required_fields=projection.missing(
                ("status", "last_update_posted")
            ),
            status=projection.values["status"],
            last_update_posted=projection.values["last_update_posted"],
            projection_sources=projection.sources,
            projection_missing_reasons=projection.reasons(
                ("status", "last_update_posted")
            ),
        ), None, None
    if domain == "file":
        projection = project_file_fact(record.arguments, raw)
        return FileCellFact(
            evidence_id=evidence_id,
            tool_name=record.tool_name,
            arguments=record.arguments,
            raw_result=record.raw_result,
            missing_required_fields=projection.missing(("file_id", "sheet", "range")),
            file_id=projection.values["file_id"],
            sheet=projection.values["sheet"],
            range=projection.values["range"],
            projection_sources=projection.sources,
            projection_missing_reasons=projection.reasons(
                ("file_id", "sheet", "range")
            ),
        ), None, None
    projection = project_regulatory_fact(raw)
    return RegulatoryRuleFact(
        evidence_id=evidence_id,
        tool_name=record.tool_name,
        arguments=record.arguments,
        raw_result=record.raw_result,
        missing_required_fields=projection.missing(("effective_date", "last_checked")),
        effective_date=projection.values["effective_date"],
        last_checked=projection.values["last_checked"],
        projection_sources=projection.sources,
        projection_missing_reasons=projection.reasons(
            ("effective_date", "last_checked")
        ),
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


def _raw_payload(raw_result: object) -> object:
    return raw_result.raw if isinstance(raw_result, ToolEnvelope) else raw_result


def _evidence_id(tool_name: str, arguments: Mapping[str, object]) -> str:
    normalized = repr(canonical_argument_key(tool_name, arguments)).encode()
    digest = hashlib.sha256(normalized).hexdigest()
    return f"v3-shadow:{tool_name}:{digest[:16]}"
