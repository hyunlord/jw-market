from __future__ import annotations

from collections.abc import Mapping
import hashlib
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from jw_chat_agent_poc import genos_config
from jw_chat_agent_poc.orchestrator.answer_contract import CONTRACT_REQUIRED_TOOLS, evaluate_answer_contract
from jw_chat_agent_poc.orchestrator.claim_policy import claim_policy_report
from jw_chat_agent_poc.orchestrator.provenance import number_tokens
from jw_chat_agent_poc.orchestrator.source_trap import requested_csd_aggregate, requested_csd_unsupported_detail, requested_unavailable_source


_UNKNOWN = "unknown"
_MODEL_FAMILY_DEFAULT = "gemini-3-flash-preview"
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
    "render_validator_version": "service/sse_protocol.py",
}


def version_payload() -> dict[str, Any]:
    """Return runtime provenance that can be exposed through /__version."""

    return {
        "release_id": _env("JW_CHAT_RELEASE_ID", "RELEASE_ID"),
        "git_sha": _env("JW_CHAT_GIT_SHA", "GIT_SHA", "COMMIT_SHA"),
        "image_digest": _env("JW_CHAT_IMAGE_DIGEST", "IMAGE_DIGEST"),
        "built_at": _env("JW_CHAT_BUILT_AT", "BUILT_AT"),
        "model_family": os.environ.get("JW_CHAT_MODEL_FAMILY", _MODEL_FAMILY_DEFAULT),
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

    markdown_response = result.get("markdown_response") if isinstance(result.get("markdown_response"), Mapping) else {}
    fact_md = _markdown_field(markdown_response, "fact_md") or _markdown_field(markdown_response, "data_md")
    claim_report = claim_policy_report(answer, fact_md)
    tools_called = _tools_called(result)
    facts_returned = _facts_returned(markdown_response)
    facts_surfaced = _facts_surfaced(answer)
    answer_contract_status = evaluate_answer_contract(question, answer, markdown_response)
    quality_taxonomy = _quality_taxonomy(
        question=question,
        result=result,
        answer=answer,
        tools_called=tools_called,
        facts_returned=facts_returned,
        facts_surfaced=facts_surfaced,
        answer_contract_status=answer_contract_status,
    )
    return {
        "trace_id": uuid4().hex,
        "conversation_id": conversation_id,
        "question": question,
        "scope": str(result.get("context_scope") or _UNKNOWN),
        "version": version_payload(),
        "intent": _intent(result),
        "route": _route(result),
        "model_stages": {
            "router_serving_id": _serving_id(genos_config.GENOS_SERVING_ID_ENV, genos_config.DEFAULT_GENOS_SERVING_ID),
            "final_serving_id": _serving_id(genos_config.GENOS_FINAL_SERVING_ID_ENV, genos_config.DEFAULT_GENOS_FINAL_SERVING_ID),
            "planner_serving_id": _serving_id(genos_config.GENOS_PLANNER_SERVING_ID_ENV, genos_config.DEFAULT_GENOS_PLANNER_SERVING_ID),
        },
        "tools_called": tools_called,
        "facts_returned": facts_returned,
        "facts_surfaced": facts_surfaced,
        "answer_contract_status": answer_contract_status,
        "quality_taxonomy": quality_taxonomy,
        "claim_policy_fact_types": claim_report["active_fact_types"],
        "claim_policy_blocks": claim_report["forbidden_claims_remaining"],
        "surface_policy_blocks": _surface_policy_blocks(result),
        "render_status": _render_status(answer),
        "ungrounded_numeric_spans": _ungrounded_numbers(answer, markdown_response),
        "token_usage": _token_usage(timing),
        "chart_count": len(charts),
        "timing_stage_count": len(timing.get("stages", ())) if isinstance(timing.get("stages"), list) else 0,
    }



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

    required_tools = _required_tools(answer_contract_status)
    missing_tools = tuple(tool for tool in required_tools if tool not in tools_called)
    if required_tools and len(missing_tools) == len(required_tools):
        return {
            "label": "not_invoked",
            "required_tools": required_tools,
            "tools_called": tuple(tools_called),
            "reason": "detected_contract_without_related_tool",
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
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        status = call.get("status")
        if isinstance(status, str) and status in _EMPTY_TOOL_STATUSES:
            calls.append({"tool": str(call.get("tool") or ""), "status": status})
    return tuple(calls)


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


def _ungrounded_numbers(answer: str, markdown_response: Mapping[str, Any]) -> tuple[str, ...]:
    allowed = markdown_response.get("allowed_numbers")
    if not isinstance(allowed, (list, tuple)):
        return ()
    allowed_set = {str(item) for item in allowed}
    return tuple(sorted(token for token in number_tokens(answer) if token not in allowed_set))


def _markdown_field(markdown_response: Mapping[str, Any], field: str) -> str:
    value = markdown_response.get(field)
    return value if isinstance(value, str) else ""
