from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any, Final

from jw_chat_agent_poc.agent_loop.bq_contracts import contract_for
from jw_chat_agent_poc.orchestrator.bq_fusion import (
    BQEvidenceSlice,
    BQFusionError,
    BQFusionMode,
    BQFusionRequest,
    SourceKind,
    validate_fusion_request,
)
from jw_chat_agent_poc.orchestrator.bq_mixed_analysis import (
    FILE_MARKET_COMPARISON_CONTRACT,
    MARKET_SOURCE_LABELS,
)

_INTERNAL_RE: Final = re.compile(
    r"\b(?:TEMP_DOCUMENT|document_id|cache_[a-z0-9_]+|jw_mart_[a-z0-9_]+)\b",
    re.IGNORECASE,
)
_SCOPES: Final = frozenset({"MARKET", "FILE", "MIXED"})
_CROSS_SOURCE_PAIRS: Final = (
    frozenset({"UBIST", "IQVIA NSA"}),
    frozenset({"FILE", "MARKET"}),
    frozenset({"FILE", "UBIST"}),
    frozenset({"FILE", "IQVIA NSA"}),
)


class BQAnalysisValidationError(ValueError):
    pass


def validate_bq_analysis_call(call: Mapping[str, Any]) -> None:
    if call.get("tool") != "bq_analysis":
        raise BQAnalysisValidationError("unexpected analysis tool")
    data = _mapping(call.get("render_data"))
    contract_id = str(data.get("contract_id") or "")
    if contract_for(contract_id) is None and contract_id != FILE_MARKET_COMPARISON_CONTRACT:
        raise BQAnalysisValidationError("unknown BQ contract")
    if not str(data.get("calculation") or "").strip():
        raise BQAnalysisValidationError("calculation identity required")
    insights = _texts(data.get("insights"))
    if not insights:
        raise BQAnalysisValidationError("non-empty insights required")
    surface = " ".join([str(call.get("summary_text") or ""), *insights])
    if _INTERNAL_RE.search(surface):
        raise BQAnalysisValidationError("internal identifier on user surface")

    labels = frozenset(_texts(data.get("source_labels")))
    if any(pair <= labels for pair in _CROSS_SOURCE_PAIRS):
        if data.get("never_aggregate_sources") is not True:
            raise BQAnalysisValidationError("cross-source analysis needs never-aggregate marker")
    if len(labels) > 1:
        _validate_fusion(data, labels)
    _validate_news(data.get("news_refs"))
    ledger = _validate_ledger(data.get("evidence_ledger"))
    _validate_analysis_refs(contract_id, data, labels, ledger)
    _validate_charts(data.get("chart_payloads"), ledger)


def _validate_news(value: Any) -> None:
    if value is None:
        return
    refs = _mappings(value)
    if len(refs) != len(value) if isinstance(value, Sequence) else True:
        raise BQAnalysisValidationError("invalid news identity collection")
    seen: set[str] = set()
    for ref in refs:
        fields = [str(ref.get(key) or "").strip() for key in ("title", "date", "source", "url")]
        if not all(fields):
            raise BQAnalysisValidationError("incomplete news identity")
        if fields[-1] in seen:
            raise BQAnalysisValidationError("duplicate news identity")
        seen.add(fields[-1])


def _validate_ledger(value: Any) -> list[Mapping[str, Any]]:
    rows = _mappings(value)
    if not rows:
        raise BQAnalysisValidationError("empty evidence ledger")
    for row in rows:
        if not all(str(row.get(key) or "").strip() for key in ("source", "kind", "identity")):
            raise BQAnalysisValidationError("incomplete evidence ledger row")
        if row.get("kind") == "tool_result":
            raise BQAnalysisValidationError("concrete evidence required")
    return rows


def _validate_charts(value: Any, ledger: list[Mapping[str, Any]]) -> None:
    if value is None:
        return
    charts = _mappings(value)
    if len(charts) != len(value) if isinstance(value, Sequence) else True:
        raise BQAnalysisValidationError("invalid chart collection")
    for chart in charts:
        scope = str(chart.get("scope") or "").strip().upper()
        if scope not in _SCOPES:
            raise BQAnalysisValidationError("explicit chart scope required")
        if scope == "MIXED":
            groups = {key: _mappings(chart.get(key)) for key in ("market", "file")}
            if not all(groups.values()):
                raise BQAnalysisValidationError("mixed chart needs market and file groups")
            for key in ("market", "file"):
                for child in groups[key]:
                    _validate_chart_body(child, ledger=ledger, require_scope=False)
            continue
        _validate_chart_body(chart, ledger=ledger, require_scope=True)


def _validate_chart_body(
    chart: Mapping[str, Any], *, ledger: list[Mapping[str, Any]], require_scope: bool
) -> None:
    if require_scope and str(chart.get("scope") or "").strip().upper() not in _SCOPES:
        raise BQAnalysisValidationError("explicit chart scope required")
    evidence_refs = _texts(chart.get("evidence_refs"))
    if not evidence_refs:
        raise BQAnalysisValidationError("chart evidence refs required")
    bound_references = _bound_references(ledger)
    if any(reference not in bound_references for reference in evidence_refs):
        raise BQAnalysisValidationError("unbound chart evidence reference")
    datasets = _mappings(chart.get("datasets"))
    if not datasets:
        raise BQAnalysisValidationError("chart datasets required")
    for dataset in datasets:
        data = dataset.get("data")
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
            raise BQAnalysisValidationError("chart data must be a sequence")


def _validate_analysis_refs(
    contract_id: str,
    data: Mapping[str, Any],
    labels: frozenset[str],
    ledger: list[Mapping[str, Any]],
) -> None:
    if contract_id == FILE_MARKET_COMPARISON_CONTRACT:
        evidence_refs = set(_texts(data.get("evidence_refs")))
        market_labels = frozenset(_texts(data.get("market_source_labels")))
        ledger_market_sources = frozenset(
            str(row.get("source") or "").strip()
            for row in ledger
            if str(row.get("source") or "").strip() != "FILE"
        )
        if (
            "FILE" not in labels
            or len(labels) < 2
            or not market_labels
            or not market_labels <= MARKET_SOURCE_LABELS
            or market_labels != labels - {"FILE"}
            or market_labels != ledger_market_sources
            or "FILE.deterministic_answer" not in evidence_refs
            or not any(not reference.startswith("FILE.") for reference in evidence_refs)
            or not evidence_refs <= _bound_references(ledger)
        ):
            raise BQAnalysisValidationError(
                "file-market evidence needs a concrete market source and source-bound references"
            )
        return
    if contract_id != "A3":
        return
    expected = {"HIRA.render_data.items.ptntCnt"}
    expected.update(
        f"{label}.render_data.brand_value_series_10pt"
        for label in labels
        if label in {"UBIST", "IQVIA NSA"}
    )
    evidence_refs = set(_texts(data.get("evidence_refs")))
    if not expected <= evidence_refs or not evidence_refs <= _bound_references(ledger):
        raise BQAnalysisValidationError("patient-ratio evidence is not source-bound")


def _bound_references(ledger: list[Mapping[str, Any]]) -> set[str]:
    return {
        reference
        for row in ledger
        for reference in _texts(row.get("references"))
    }


def _validate_fusion(data: Mapping[str, Any], labels: frozenset[str]) -> None:
    if data.get("fusion_mode") != "side_by_side":
        raise BQAnalysisValidationError("cross-source analysis needs side-by-side fusion")
    slices = tuple(_slice(label) for label in sorted(labels))
    try:
        validate_fusion_request(BQFusionRequest(BQFusionMode.SIDE_BY_SIDE, slices))
    except BQFusionError as exc:
        raise BQAnalysisValidationError(str(exc)) from exc


def _slice(label: str) -> BQEvidenceSlice:
    normalized = label.casefold()
    if label == "FILE":
        return BQEvidenceSlice.file(label, (f"{label}:evidence",))
    if label == "CSD":
        return BQEvidenceSlice.csd(label, "unknown", (f"{label}:evidence",))
    if label == "HIRA":
        return BQEvidenceSlice.hira(label, "unknown", "unknown", (f"{label}:evidence",))
    if label in {"NEWS", "WEB"}:
        return BQEvidenceSlice.news(label, (f"{label}:evidence",))
    if "ubist" in normalized or "iqvia" in normalized or label == "MARKET":
        return BQEvidenceSlice.market(
            label, "unknown", "unknown", "unknown", "unknown", (f"{label}:evidence",)
        )
    return BQEvidenceSlice(SourceKind.NEWS, label, None, None, None, None, None, (f"{label}:evidence",))


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BQAnalysisValidationError("analysis render_data required")
    return value


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _texts(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
