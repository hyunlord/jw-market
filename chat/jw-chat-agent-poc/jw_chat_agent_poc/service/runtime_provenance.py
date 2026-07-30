from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from jw_chat_agent_poc import genos_config
from jw_chat_agent_poc.agent_loop.bq_contracts import BQ_CONTRACT_IDS
from jw_chat_agent_poc.agent_loop.element_ledger import disposition_from_ledger
from jw_chat_agent_poc.agent_loop.structured_planner import STRUCTURED_PLAN_KINDS
from jw_chat_agent_poc.common.timing import internal_latency_payload
from jw_chat_agent_poc.orchestrator.agent import QueryFailureReason
from jw_chat_agent_poc.orchestrator.answer_contract import CONTRACT_REQUIRED_TOOLS, evaluate_answer_contract
from jw_chat_agent_poc.orchestrator.claim_policy import claim_policy_report
from jw_chat_agent_poc.orchestrator.provenance import number_tokens
from jw_chat_agent_poc.orchestrator.source_trap import requested_csd_aggregate, requested_csd_unsupported_detail, requested_unavailable_source
from jw_chat_agent_poc.service.conversation_context import ReferenceRecogniser, ReferenceStatus
from jw_chat_agent_poc.service.failure_disposition import failure_kind as detect_failure_kind
from jw_chat_agent_poc.service.answer_delivery import project_answer_delivery
from jw_chat_agent_poc.service.runtime_numeric_grounding import ungrounded_numbers as _ungrounded_numbers
from jw_chat_agent_poc.service.routing_v4_trace import project_routing_v4_qa_trace


_UNKNOWN = "unknown"
_UNREGISTERED = "other"
_BQ_KIND_PREFIX = "BQ:"
_PLAN_FAMILY_NONE = "none"
_PLAN_FAMILY_BQ = "bq"
_PLAN_FAMILY_STRUCTURED = "structured"
#: Sources a BQ contract can declare as expected, per bq_planner._SOURCE_VARIANTS.
_PLAN_SOURCE_ALLOW = frozenset({"ubist", "iqvia_nsa"})
#: Failure reasons a tool call may carry. Derived from the enum that writes them
#: so a new reason cannot silently project as "other", plus the typed absence
#: code the prescription contract emits.
_REASON_CODE_ALLOW = frozenset({reason.value for reason in QueryFailureReason} | {"FIELD_NOT_EXPOSED"})
#: How the anaphora resolver ended, and which recogniser claimed the question.
#: Both are derived from the enums that write them, so a new value cannot
#: silently project as "other".
_REFERENCE_STATUS_ALLOW = frozenset({status.value for status in ReferenceStatus})
_RECOGNISER_ALLOW = frozenset({recogniser.value for recogniser in ReferenceRecogniser})
_MODEL_FAMILY_DEFAULT = "gemini-3-flash-preview"
_MODEL_FAMILY_ENVS = {
    "router": "JW_CHAT_ROUTER_MODEL_FAMILY",
    "final": "JW_CHAT_FINAL_MODEL_FAMILY",
    "planner": "JW_CHAT_PLANNER_MODEL_FAMILY",
}
_EMPTY_TOOL_STATUSES = frozenset({"no_data", "unsupported", "error"})
_ASSEMBLY_GAP_RATIO_THRESHOLD = 0.30
_ASSEMBLY_GAP_MIN_FACT_CHARS = 500
_FIELD_MISSING_STATUSES = frozenset({"missing_fact_set", "missing_required_fact", "insufficient_rows"})

_BROKEN_RENDER_SENTINELS = (
    "|| ---",
    "|##",
    "억원 |##",
    '{"kind":"table"',
    '"markdown":"',
)
_VERSIONED_FILES = {
    "prompt_version": "service/genos_client.py",
    "routing_registry_version": "agent_loop/population_specs.py",
    "claim_policy_version": "orchestrator/claim_policy.py",
    "surface_policy_version": "orchestrator/surface_policy.py",
    "answer_contract_version": "orchestrator/answer_contract.py",
    "response_format_contract_version": "orchestrator/response_format_contract.py",
    "render_validator_version": "service/sse_protocol.py",
}


def version_payload() -> dict[str, Any]:
    """Return runtime provenance that can be exposed through /__version."""

    model_family = os.environ.get("JW_CHAT_MODEL_FAMILY", _MODEL_FAMILY_DEFAULT)
    return {
        "release_id": _env("JW_CHAT_RELEASE_ID", "RELEASE_ID"),
        "git_sha": _env("JW_CHAT_GIT_SHA", "GIT_SHA", "COMMIT_SHA"),
        "image_digest": _env("JW_CHAT_IMAGE_DIGEST", "IMAGE_DIGEST"),
        "built_at": _env("JW_CHAT_BUILT_AT", "BUILT_AT"),
        "model_family": model_family,
        "model_families": _model_families(model_family),
        "serving_common_router": _serving_id(
            genos_config.GENOS_SERVING_ID_ENV,
            genos_config.DEFAULT_GENOS_SERVING_ID,
        ),
        "serving_final": _serving_id(
            genos_config.GENOS_FINAL_SERVING_ID_ENV,
            genos_config.DEFAULT_GENOS_FINAL_SERVING_ID,
        ),
        "serving_planner": _serving_id(
            genos_config.GENOS_PLANNER_SERVING_ID_ENV,
            genos_config.DEFAULT_GENOS_PLANNER_SERVING_ID,
        ),
        "policy_versions": _policy_versions(),
    }


def trace_envelope(
    *,
    question: str,
    result: Mapping[str, Any],
    answer: str,
    charts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    timing: Mapping[str, Any],
    conversation_id: str | None,
) -> dict[str, Any]:
    """Build request-local trace metadata without changing the public answer body."""

    trace_id = uuid4().hex
    version = version_payload()
    markdown_response = result.get("markdown_response") if isinstance(result.get("markdown_response"), Mapping) else {}
    fact_md = _markdown_field(markdown_response, "fact_md") or _markdown_field(markdown_response, "data_md")
    claim_report = claim_policy_report(answer, fact_md)
    tools_called = _tools_called(result)
    facts_returned = _facts_returned(markdown_response)
    facts_surfaced = _facts_surfaced(answer)
    answer_contract_status = evaluate_answer_contract(
        question,
        answer,
        markdown_response,
        tool_calls=tuple(
            call for call in (result.get("tool_calls") or ()) if isinstance(call, Mapping)
        ),
    )
    quality_taxonomy = _quality_taxonomy(
        question=question,
        result=result,
        answer=answer,
        tools_called=tools_called,
        facts_returned=facts_returned,
        facts_surfaced=facts_surfaced,
        answer_contract_status=answer_contract_status,
    )
    model_families = version["model_families"]
    return {
        "trace_id": trace_id,
        "conversation_id": conversation_id,
        "question": question,
        "scope": str(result.get("context_scope") or _UNKNOWN),
        "version": version,
        "intent": _intent(result),
        "route": _route(result),
        "model_stages": {
            "router_serving_id": _serving_id(genos_config.GENOS_SERVING_ID_ENV, genos_config.DEFAULT_GENOS_SERVING_ID),
            "router_model_family": model_families["router"],
            "final_serving_id": _serving_id(genos_config.GENOS_FINAL_SERVING_ID_ENV, genos_config.DEFAULT_GENOS_FINAL_SERVING_ID),
            "final_model_family": model_families["final"],
            "planner_serving_id": _serving_id(genos_config.GENOS_PLANNER_SERVING_ID_ENV, genos_config.DEFAULT_GENOS_PLANNER_SERVING_ID),
            "planner_model_family": model_families["planner"],
        },
        "tools_called": tools_called,
        "facts_returned": facts_returned,
        "facts_surfaced": facts_surfaced,
        "answer_contract_status": answer_contract_status,
        "quality_taxonomy": quality_taxonomy,
        "claim_policy_fact_types": claim_report["active_fact_types"],
        "claim_policy_blocks": claim_report["forbidden_claims_remaining"],
        "surface_policy_blocks": _surface_policy_blocks(result),
        "response_format_contract": _response_format_contract(result),
        "render_status": _render_status(answer),
        "ungrounded_numeric_spans": _ungrounded_numbers(
            answer,
            _numeric_grounding_response(result, markdown_response),
            result.get("tool_calls") if isinstance(result.get("tool_calls"), list) else (),
            question=question,
        ),
        "token_usage": _token_usage(timing),
        "chart_count": len(charts),
        "timing_stage_count": len(timing.get("stages", ())) if isinstance(timing.get("stages"), list) else 0,
        "qa_trace": _qa_trace(
            trace_id=trace_id,
            conversation_id=conversation_id,
            result=result,
            answer=answer,
            version=version,
        ),
    }


def _response_format_contract(result: Mapping[str, Any]) -> dict[str, Any]:
    report = result.get("_response_format_contract")
    return dict(report) if isinstance(report, Mapping) else {}


def _qa_trace(
    *,
    trace_id: str,
    conversation_id: str | None,
    result: Mapping[str, Any],
    answer: str,
    version: Mapping[str, Any],
) -> dict[str, Any]:
    diagnostics = result.get("router_diagnostics")
    diagnostic_items = diagnostics if isinstance(diagnostics, Mapping) else {}
    gate = str(diagnostic_items.get("gate") or diagnostic_items.get("mode") or "none")
    gate_reason = str(
        diagnostic_items.get("gate_reason") or diagnostic_items.get("reason") or ""
    ) or None
    claim_gate = result.get("_qa_claim_gate")
    claim_items = claim_gate if isinstance(claim_gate, Mapping) else {}
    disposition = str(claim_items.get("disposition") or "")
    detected_failure_kind = str(claim_items.get("failure_kind") or "") or detect_failure_kind(
        answer,
        tuple(
            call
            for call in (result.get("tool_calls") or ())
            if isinstance(call, Mapping)
        ),
    )
    if detected_failure_kind and disposition in {"", "answered"}:
        disposition = "unavailable"
    if not disposition:
        # Prefer what was actually delivered per requested element. A non-empty
        # body is not evidence that the request was served: a request whose only
        # element was refused still carries the refusal notice as its body.
        ledger = result.get("element_ledger")
        aggregated = (
            disposition_from_ledger(tuple(ledger))
            if isinstance(ledger, (list, tuple))
            else None
        )
        disposition = aggregated or ("empty" if not answer.strip() else "answered")
    final = {
        "disposition": disposition,
        "body_empty": not bool(answer.strip()),
    }
    if detected_failure_kind:
        final["failure_kind"] = detected_failure_kind
    claim_trace = {
        "blocked_count": int(claim_items.get("blocked_claim_count") or 0),
        "blocked_reasons": tuple(
            str(item)
            for item in claim_items.get("blocked_reasons", ())
            if str(item)
        ),
    }
    rejection_trace = tuple(
        dict(item)
        for item in claim_items.get("rejections", ())
        if isinstance(item, Mapping)
    )
    if rejection_trace:
        claim_trace["rejections"] = rejection_trace
    binding_decision = claim_items.get("binding_decision")
    if isinstance(binding_decision, Mapping):
        claim_trace["binding_decision"] = dict(binding_decision)
    pipeline_observability = claim_items.get("pipeline_observability")
    if isinstance(pipeline_observability, Mapping):
        claim_trace["pipeline_observability"] = dict(pipeline_observability)
    for key in ("binder_input_context", "pre_binding_answer_context"):
        context_observability = claim_items.get(key)
        if isinstance(context_observability, Mapping):
            claim_trace[key] = dict(context_observability)
    qa_trace = {
        "request": {
            "request_id": trace_id,
            "session_id": conversation_id,
            "pod": os.environ.get("HOSTNAME") or _UNKNOWN,
            "image_revision": str(version.get("git_sha") or version.get("release_id") or _UNKNOWN),
        },
        "routing": {
            "scope": str(
                result.get("context_scope")
                or diagnostic_items.get("scope")
                or _UNKNOWN
            ),
            "route": _route(result),
            "gate": gate,
            "gate_reason": gate_reason,
            "anaphora": _qa_anaphora(result),
        },
        "tools": _qa_tool_calls(result),
        "plan": _qa_plan(result),
        "spans": _qa_spans(result),
        "latency": internal_latency_payload(
            result.get("timing") if isinstance(result.get("timing"), Mapping) else None
        ),
        "claims": claim_trace,
        "answer_delivery": project_answer_delivery(result),
        "input_policy_decision": _security_decision(
            result,
            "_sec12_input_policy_decision",
        ),
        "output_leakage_decision": _security_decision(
            result,
            "_sec12_output_leakage_decision",
        ),
        "user_surface_action": _user_surface_action(result),
        "final": final,
    }
    routing_v4 = project_routing_v4_qa_trace(diagnostic_items)
    if routing_v4 is not None:
        qa_trace["routing_v4"] = routing_v4
    return qa_trace


def _security_decision(
    result: Mapping[str, Any],
    key: str,
) -> dict[str, Any]:
    raw = result.get(key)
    decision = raw if isinstance(raw, Mapping) else {}
    reasons = decision.get("reason_codes")
    return {
        "mode": str(decision.get("mode") or "shadow"),
        "verdict": str(decision.get("verdict") or "not_evaluated"),
        "reason_codes": tuple(
            str(reason)
            for reason in (reasons if isinstance(reasons, (list, tuple)) else ())
            if str(reason)
        ),
    }


def _user_surface_action(result: Mapping[str, Any]) -> str:
    for key in ("_sec12_output_leakage_decision", "_sec12_input_policy_decision"):
        raw = result.get(key)
        decision = raw if isinstance(raw, Mapping) else {}
        action = str(decision.get("user_surface_action") or "none")
        if action != "none":
            return action if action in {"observe_only", "blocked"} else "other"
    return "none"


def _qa_anaphora(result: Mapping[str, Any]) -> dict[str, Any]:
    """Project how the previous-turn resolver ended, before the router ran.

    ``unresolved_reference`` alone could not answer this: it is ``False`` both
    for a question that needed no resolving and for a bare follow-up the
    resolver had no vocabulary for, and the second case reaches the router as an
    unrewritten string whose subject only existed in the previous turn. Every
    key is emitted unconditionally so "not observed" (null) stays distinct from
    "no reference" (``not_anaphoric``).
    """
    items = result.get("_qa_anaphora")
    observation = items if isinstance(items, Mapping) else {}
    status = observation.get("status")
    recogniser = observation.get("recogniser")
    unresolved = observation.get("unresolved_reference")
    shape = observation.get("candidate_shape")
    inherited = observation.get("inherited_issue_observation")
    return {
        "status": (
            (status if status in _REFERENCE_STATUS_ALLOW else _UNREGISTERED)
            if isinstance(status, str) and status
            else None
        ),
        "recogniser": (
            (recogniser if recogniser in _RECOGNISER_ALLOW else _UNREGISTERED)
            if isinstance(recogniser, str) and recogniser
            else None
        ),
        "candidate_shape": shape if isinstance(shape, bool) else None,
        "unresolved_reference": unresolved if isinstance(unresolved, bool) else None,
        # Whether a cause question took over the previous turn's news observation.
        # The resolver has emitted this since GPT5-FIX-P3, but this projection dropped
        # it, so the one field that separates an inherited cause question from an
        # identical standalone one was invisible in the live trace. A bool, like the
        # two above it — the headlines themselves are content and stay out of here.
        "inherited_issue_observation": inherited if isinstance(inherited, bool) else None,
    }


def _qa_plan(result: Mapping[str, Any]) -> dict[str, Any]:
    """Project which deterministic contract ran, and what it could not reach.

    The loop already recorded all of this in ``agent_loop_metrics``; nothing here
    read it, so a live answer could not be checked against the contract that was
    supposed to produce it. Every key is emitted unconditionally, so "not
    observed" (null) stays distinct from "no deterministic plan ran" (family
    ``none``). Kinds and source names are confirmed against their registries, so
    the projection can only ever carry enumerated values.
    """
    metrics = result.get("agent_loop_metrics")
    items = metrics if isinstance(metrics, Mapping) else {}
    raw_kind = items.get("deterministic_plan_kind")
    kind = raw_kind if isinstance(raw_kind, str) and raw_kind else None
    raw_hit = items.get("deterministic_plan_hit")
    return {
        "family": _plan_family(kind),
        "kind": _plan_kind(kind),
        "hit": bool(raw_hit) if isinstance(raw_hit, bool) else None,
        "missing_sources": _plan_missing_sources(items.get("bq_missing_sources")),
    }


def _plan_family(kind: str | None) -> str:
    if kind is None:
        return _PLAN_FAMILY_NONE
    return _PLAN_FAMILY_BQ if kind.startswith(_BQ_KIND_PREFIX) else _PLAN_FAMILY_STRUCTURED


def _plan_kind(kind: str | None) -> str | None:
    if kind is None:
        return None
    if kind.startswith(_BQ_KIND_PREFIX):
        return kind if kind.removeprefix(_BQ_KIND_PREFIX) in set(BQ_CONTRACT_IDS) else _UNREGISTERED
    return kind if kind in STRUCTURED_PLAN_KINDS else _UNREGISTERED


def _plan_missing_sources(value: Any) -> list[str] | None:
    if not isinstance(value, (list, tuple)):
        return None
    return [str(source) if str(source) in _PLAN_SOURCE_ALLOW else _UNREGISTERED for source in value]


def _qa_spans(result: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    spans = result.get("_qa_spans")
    if not isinstance(spans, list):
        return ()
    public_keys = (
        "name",
        "category",
        "detail",
        "started_at",
        "ended_at",
        "elapsed_ms",
        "status",
    )
    projected: list[dict[str, Any]] = []
    for span in spans:
        if not isinstance(span, Mapping):
            continue
        projected.append({key: span.get(key) for key in public_keys})
    return tuple(projected)


def _qa_tool_calls(result: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    calls = result.get("tool_calls")
    if not isinstance(calls, list):
        return ()
    projected: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        trace = call.get("qa_trace")
        trace_items = trace if isinstance(trace, Mapping) else {}
        render_data = call.get("render_data")
        render_items = render_data if isinstance(render_data, Mapping) else {}
        projected.append(
            {
                "name": _public_tool_name(call) if isinstance(call.get("tool"), str) else _UNKNOWN,
                "started_at": trace_items.get("started_at"),
                "ended_at": trace_items.get("ended_at"),
                "status": trace_items.get("status") or call.get("status") or render_items.get("status") or _UNKNOWN,
                "row_count": trace_items.get("row_count"),
                "data_as_of": trace_items.get("data_as_of"),
                "cache_hit": bool(trace_items.get("cache_hit")),
                "endpoint": trace_items.get("endpoint"),
                "latency_ms": trace_items.get("latency_ms"),
                "source_epoch": trace_items.get("source_epoch"),
                "built_at": trace_items.get("built_at"),
                # General-view ATC4 selection provenance. These are written onto
                # call["qa_trace"] by general_view_routing._attach_selection_trace and
                # were previously dropped here, which made it impossible to tell whether
                # a live answer used the catalog definition or fell back to brand
                # membership. Emitted unconditionally so that "not observed" (key absent)
                # and "not applicable" (key present, null) stay distinguishable.
                "input_market": trace_items.get("input_market"),
                "atc4_source": trace_items.get("atc4_source"),
                "candidate_atc4_codes": _atc4_code_list(trace_items.get("candidate_atc4_codes")),
                "member_brand_count": _optional_int(trace_items.get("member_brand_count")),
                "excluded_atc4_count": _optional_int(trace_items.get("excluded_atc4_count")),
                "reduction_reason": trace_items.get("reduction_reason"),
                # Why a metric query failed. agent._query_failed_metric_call has
                # always written this onto render_data; this projection read
                # render_data for "status" alone, so the reason never left the
                # process. Emitted unconditionally, so an unobserved reason is a
                # null rather than an absent key.
                "reason_code": _reason_code(render_items.get("reason_code")),
            }
        )
    return tuple(projected)


def _reason_code(value: Any) -> str | None:
    """Project a failure reason only when it is one of the codes we define."""
    if not isinstance(value, str) or not value:
        return None
    return value if value in _REASON_CODE_ALLOW else _UNREGISTERED


def _atc4_code_list(value: Any) -> list[str] | None:
    """Project ATC4 candidates as a bare code list, never free text."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        return None
    return [str(code) for code in value]


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _numeric_grounding_response(
    result: Mapping[str, Any],
    markdown_response: Mapping[str, Any],
) -> Mapping[str, Any]:
    grounding_text = result.get("file_brief_grounding_text")
    if result.get("file_only_ready") is True and isinstance(grounding_text, str):
        return {"fact_md": grounding_text}
    return markdown_response



def _quality_taxonomy(
    *,
    question: str,
    result: Mapping[str, Any],
    answer: str,
    tools_called: list[str],
    facts_returned: Mapping[str, Any],
    facts_surfaced: Mapping[str, Any],
    answer_contract_status: Mapping[str, Any],
) -> dict[str, Any]:
    diagnostics = result.get("router_diagnostics")
    source = requested_unavailable_source(
        question,
        identity_only=isinstance(diagnostics, Mapping)
        and diagnostics.get("mode") == "tool_use_agent",
    )
    if source is not None:
        return {
            "label": "not_connected",
            "source": source.key,
            "source_label": source.label,
            "reason": "requested_source_registry_match",
        }
    if _requested_csd_source(question):
        has_successful_csd_activity = _has_successful_csd_activity(result)
        if has_successful_csd_activity:
            if requested_csd_unsupported_detail(question):
                return {
                    "label": "fields_missing",
                    "source": "csd",
                    "source_label": "CSD 영업활동",
                    "reason": "csd_aggregate_connected_but_detail_fields_missing",
                    "available_fields": ("period_ym", "market", "jw_channel", "master_product", "product_details"),
                    "missing_fields": ("impact level", "HCP/의사별", "기관별"),
                }
        elif requested_csd_aggregate(question) and "csd_activity_trend" not in tools_called:
            return {
                "label": "not_invoked",
                "required_tools": ("csd_activity_trend",),
                "tools_called": tuple(tools_called),
                "reason": "csd_aggregate_tool_not_invoked",
            }
        elif requested_csd_unsupported_detail(question):
            return {
                "label": "fields_missing",
                "source": "csd",
                "source_label": "CSD 영업활동",
                "reason": "csd_detail_fields_not_available",
                "missing_fields": ("impact level", "HCP/의사별", "기관별"),
            }
        if not has_successful_csd_activity and not requested_csd_aggregate(question) and not requested_csd_unsupported_detail(question):
            return {
                "label": "not_connected",
                "source": "csd",
                "source_label": "CSD 영업활동",
                "reason": "requested_unconnected_csd_source",
            }

    empty_calls = _empty_result_calls(result)
    if empty_calls:
        return {"label": "empty_result", "calls": empty_calls, "reason": "tool_status_empty"}

    contract_status = str(answer_contract_status.get("status") or "")
    if contract_status in _FIELD_MISSING_STATUSES:
        return {
            "label": "fields_missing",
            "answer_contract_status": dict(answer_contract_status),
            "reason": "answer_contract_required_fields_missing",
        }

    assembly_gap = _assembly_gap_reason(answer, facts_returned, facts_surfaced, answer_contract_status)
    if assembly_gap:
        if _is_low_surface_ratio_warning(assembly_gap):
            return {"label": "low_surface_ratio_warning", **assembly_gap}
        return {"label": "assembly_gap_suspected", **assembly_gap}

    return {"label": "na", "reason": "no_quality_taxonomy_signal"}


def _is_low_surface_ratio_warning(assembly_gap: Mapping[str, Any]) -> bool:
    return (
        assembly_gap.get("low_surface_ratio") is True
        and assembly_gap.get("marker_missing") is False
        and assembly_gap.get("contract_status") == "pass"
    )


def _requested_csd_source(question: str) -> bool:
    lowered = question.lower()
    return "csd" in lowered or requested_csd_aggregate(question) or requested_csd_unsupported_detail(question)


def _has_successful_csd_activity(result: Mapping[str, Any]) -> bool:
    tool_calls = result.get("tool_calls")
    if not isinstance(tool_calls, list):
        return False
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        if call.get("tool") != "csd_activity_trend":
            continue
        data = call.get("render_data")
        if isinstance(data, Mapping) and data.get("status") == "ok":
            return True
        if call.get("status") == "ok":
            return True
    return False


def _required_tools(answer_contract_status: Mapping[str, Any]) -> tuple[str, ...]:
    structural = answer_contract_status.get("structural_contract")
    if isinstance(structural, str) and structural:
        return CONTRACT_REQUIRED_TOOLS.get(structural, ())
    intent = answer_contract_status.get("intent")
    if isinstance(intent, str) and intent:
        return CONTRACT_REQUIRED_TOOLS.get(intent, ())
    return ()


def _empty_result_calls(result: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    calls: list[dict[str, str]] = []
    tool_calls = result.get("tool_calls")
    if not isinstance(tool_calls, list):
        return ()
    recovered_tools = {
        str(call.get("tool") or "")
        for call in tool_calls
        if isinstance(call, Mapping) and _call_has_evidence(call)
    }
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        status = call.get("status")
        tool = str(call.get("tool") or "")
        if isinstance(status, str) and status in _EMPTY_TOOL_STATUSES and tool not in recovered_tools:
            calls.append({"tool": str(call.get("tool") or ""), "status": status})
    return tuple(calls)


def _call_has_evidence(call: Mapping[str, Any]) -> bool:
    if call.get("status") != "ok":
        return False
    render_data = call.get("render_data")
    if not isinstance(render_data, Mapping) or render_data.get("ok") is False:
        return False
    evidence = render_data.get("evidence")
    return isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes)) and bool(evidence)


def _assembly_gap_reason(
    answer: str,
    facts_returned: Mapping[str, Any],
    facts_surfaced: Mapping[str, Any],
    answer_contract_status: Mapping[str, Any],
) -> dict[str, Any]:
    fact_chars = _int_value(facts_returned.get("fact_md_chars"))
    data_chars = _int_value(facts_returned.get("data_md_chars"))
    returned_chars = fact_chars + data_chars
    answer_chars = _int_value(facts_surfaced.get("answer_chars"))
    ratio = (answer_chars / returned_chars) if returned_chars else 0.0
    contract_status = str(answer_contract_status.get("status") or "")
    structural = str(answer_contract_status.get("structural_contract") or "")
    marker_missing = bool(structural and contract_status != "pass")
    low_surface_ratio = returned_chars >= _ASSEMBLY_GAP_MIN_FACT_CHARS and ratio < _ASSEMBLY_GAP_RATIO_THRESHOLD
    if not marker_missing and not low_surface_ratio:
        return {}
    return {
        "reason": "contract_marker_missing_or_low_surface_ratio",
        "returned_chars": returned_chars,
        "answer_chars": answer_chars,
        "surface_ratio": round(ratio, 4),
        "threshold": _ASSEMBLY_GAP_RATIO_THRESHOLD,
        "min_fact_chars": _ASSEMBLY_GAP_MIN_FACT_CHARS,
        "contract_status": contract_status,
        "structural_contract": structural,
        "marker_missing": marker_missing,
        "low_surface_ratio": low_surface_ratio,
    }


def _int_value(value: Any) -> int:
    return value if isinstance(value, int) else 0

def _token_usage(timing: Mapping[str, Any]) -> dict[str, Any]:
    usage = timing.get("token_usage")
    if isinstance(usage, dict):
        return usage
    return {"available": False, "calls": [], "total_input_tokens": 0, "total_output_tokens": 0, "total_tokens": 0}


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return _UNKNOWN


def _model_families(legacy_family: str) -> dict[str, str]:
    return {
        stage: os.environ.get(env_name) or legacy_family
        for stage, env_name in _MODEL_FAMILY_ENVS.items()
    }


def _serving_id(env_name: str, default: str) -> str:
    return os.environ.get(env_name) or os.environ.get(genos_config.GENOS_SERVING_ID_ENV) or default


def _policy_versions() -> dict[str, str]:
    return {name: _source_hash(relative_path) for name, relative_path in _VERSIONED_FILES.items()}


def _source_hash(relative_path: str) -> str:
    root = Path(__file__).resolve().parents[1]
    path = root / relative_path
    if not path.is_file():
        return "missing"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return f"sha256:{digest}"


def _intent(result: Mapping[str, Any]) -> dict[str, Any]:
    decomposition = result.get("decomposition")
    if not isinstance(decomposition, list):
        return {"items": ()}
    items: list[dict[str, Any]] = []
    for item in decomposition:
        if isinstance(item, Mapping):
            items.append(
                {
                    "intent": item.get("intent"),
                    "status": item.get("status"),
                    "max_steps": item.get("max_steps"),
                }
            )
    return {"items": tuple(items)}


def _route(result: Mapping[str, Any]) -> dict[str, Any]:
    diagnostics = result.get("router_diagnostics")
    if not isinstance(diagnostics, Mapping):
        return {"mode": _UNKNOWN}
    route = {
        "mode": diagnostics.get("mode", _UNKNOWN),
        "deterministic_execution": diagnostics.get("deterministic_execution"),
    }
    if diagnostics.get("route") is not None:
        route["route"] = diagnostics.get("route")
    if diagnostics.get("tool_execution_mode") is not None:
        route["tool_execution_mode"] = diagnostics.get("tool_execution_mode")
    if diagnostics.get("parallel_tool_count") is not None:
        route["parallel_tool_count"] = diagnostics.get("parallel_tool_count")
    return route


def _tools_called(result: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    tool_calls = result.get("tool_calls")
    if not isinstance(tool_calls, list):
        return names
    for call in tool_calls:
        if isinstance(call, Mapping) and isinstance(call.get("tool"), str):
            names.append(_public_tool_name(call))
    return names


def _public_tool_name(call: Mapping[str, Any]) -> str:
    name = str(call["tool"])
    render_data = call.get("render_data")
    if name in {"query_failed", "unsupported_metric"} and isinstance(render_data, Mapping):
        actual_name = str(render_data.get("tool_name") or "").strip()
        if actual_name:
            return actual_name
    if name == "get_brand_metric" and isinstance(render_data, Mapping) and render_data.get("metric") == "query_spec":
        return "query_spec"
    if name == "get_market_landscape":
        return "market_scope"
    if name == "deep_analysis_related_news" and isinstance(render_data, Mapping) and _is_public_news_search(render_data):
        return "search_news"
    return name


def _is_public_news_search(render_data: Mapping[str, Any]) -> bool:
    facade_tool = render_data.get("facade_tool")
    if facade_tool in {"search_news", "background_news_context"}:
        return True
    if render_data.get("context_role") == "background_insight":
        return False
    items = render_data.get("items")
    return isinstance(items, list) and bool(items)


def _facts_returned(markdown_response: Mapping[str, Any]) -> dict[str, Any]:
    fact_md = _markdown_field(markdown_response, "fact_md")
    data_md = _markdown_field(markdown_response, "data_md")
    evidence = markdown_response.get("evidence")
    return {
        "fact_md_chars": len(fact_md),
        "data_md_chars": len(data_md),
        "fact_table_count": fact_md.count("\n|"),
        "evidence_count": len(evidence) if isinstance(evidence, list) else 0,
    }


def _facts_surfaced(answer: str) -> dict[str, Any]:
    return {
        "answer_chars": len(answer),
        "table_count": answer.count("\n|"),
        "numeric_token_count": len(number_tokens(answer)),
    }


def _surface_policy_blocks(result: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    blocks: list[dict[str, Any]] = []
    for call in result.get("tool_calls", []) if isinstance(result.get("tool_calls"), list) else []:
        if not isinstance(call, Mapping):
            continue
        data = call.get("render_data")
        if not isinstance(data, Mapping):
            continue
        policy = data.get("surface_policy")
        if isinstance(policy, Mapping):
            for key, value in policy.items():
                blocks.append({"tool": call.get("tool"), "field": key, "status": value})
    return tuple(blocks)


def _render_status(answer: str) -> dict[str, Any]:
    issues = [f"sentinel:{marker}" for marker in _BROKEN_RENDER_SENTINELS if marker in answer]
    issues.extend(_table_cell_count_issues(answer))
    return {"status": "pass" if not issues else "fail", "issues": tuple(issues)}


def _table_cell_count_issues(answer: str) -> list[str]:
    issues: list[str] = []
    lines = answer.replace("\r\n", "\n").splitlines()
    index = 0
    while index < len(lines):
        if _is_table_start(lines, index):
            expected = _cell_count(lines[index])
            row_index = index + 1
            while row_index < len(lines) and _is_table_row(lines[row_index]):
                current = _cell_count(lines[row_index])
                if current != expected:
                    issues.append(f"table_cell_count:line={row_index + 1}:expected={expected}:actual={current}")
                row_index += 1
            index = row_index
            continue
        index += 1
    return issues


def _is_table_start(lines: list[str], index: int) -> bool:
    return _is_table_row(lines[index]) and index + 1 < len(lines) and set(lines[index + 1].replace("|", "").strip()) <= {"-", ":", " "}


def _is_table_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def _cell_count(line: str) -> int:
    stripped = line.strip().strip("|")
    if not stripped:
        return 0
    return len(stripped.split("|"))


def _markdown_field(markdown_response: Mapping[str, Any], field: str) -> str:
    value = markdown_response.get(field)
    return value if isinstance(value, str) else ""
