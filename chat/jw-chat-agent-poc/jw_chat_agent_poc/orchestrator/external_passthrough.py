from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
import re
from typing import Any, Final

from jw_chat_agent_poc.common.timing import Timing, stage
from jw_chat_agent_poc.tools.external import ExternalApiClient


EXTERNAL_PASSTHROUGH_FIELD: Final = "_external_passthrough"
WEB_FALLBACK_DISCLOSURE: Final = "공식 소스에서 확인하지 못해 웹 검색 결과로 답합니다"

_EXTERNAL_TOOL_PREFIXES: Final[tuple[str, ...]] = (
    "hira_",
    "mfds_",
    "nedrug_",
    "clinicaltrials_",
    "openfda_",
    "news_",
)
_EXTERNAL_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "search_news",
        "web_search",
    }
)
_OFFICIAL_TOOL_PREFIXES: Final[tuple[str, ...]] = (
    "hira_",
    "mfds_",
    "nedrug_",
    "clinicaltrials_",
    "openfda_",
)
_FAILED_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "empty",
        "error",
        "failed",
        "failure",
        "inapplicable",
        "missing_key",
        "no_data",
        "query_failed",
        "unavailable",
        "unsupported",
    }
)
_INTERNAL_STRUCTURED_QUESTION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:\bUBIST\b|\bIQVIA\b|\bCSD\b|\bHHI\b|시장\s*규모|시장\s*점유|점유율|"
    r"매출|처방|시장\s*순위|브랜드\s*순위|경쟁\s*구도|채널|진료과|"
    r"market\s*share|sales)",
    re.IGNORECASE,
)


def is_external_passthrough_tool(tool: object) -> bool:
    normalized = str(tool or "").strip().casefold()
    return normalized in _EXTERNAL_TOOL_NAMES or normalized.startswith(_EXTERNAL_TOOL_PREFIXES)


def external_passthrough_eligible(
    question: str,
    tool_calls: Sequence[Mapping[str, object]],
) -> bool:
    if _INTERNAL_STRUCTURED_QUESTION_RE.search(question):
        return False
    return any(is_external_passthrough_tool(call.get("tool")) for call in tool_calls)


def prepare_external_passthrough(
    question: str,
    payload: dict[str, Any],
    *,
    external: ExternalApiClient,
    timing: Timing | None = None,
) -> dict[str, Any]:
    raw_calls = payload.get("tool_calls")
    if not isinstance(raw_calls, list):
        return payload
    calls = [dict(call) for call in raw_calls if isinstance(call, Mapping)]
    diagnostic_tools = _diagnostic_external_tools(payload)
    if _INTERNAL_STRUCTURED_QUESTION_RE.search(question) or not (
        any(is_external_passthrough_tool(call.get("tool")) for call in calls)
        or diagnostic_tools
    ):
        return payload

    observed_at = datetime.now(timezone.utc).isoformat()
    calls = [
        {**call, "queried_at_utc": call.get("queried_at_utc") or observed_at}
        for call in calls
    ]
    called_tools = {
        str(call.get("tool") or "").strip().casefold()
        for call in calls
    }
    failed_official_tools = tuple(
        dict.fromkeys(
            [
                str(call.get("tool") or "")
                for call in calls
                if _is_official_tool(call) and _call_needs_web_fallback(call)
            ]
            + [
                tool
                for tool in diagnostic_tools
                if tool.casefold() not in called_tools and _payload_failed(payload)
            ]
        )
    )
    web_fallback_attempted = bool(failed_official_tools)
    usable_web_present = any(_usable_web_call(call) for call in calls)
    web_fallback_used = web_fallback_attempted and usable_web_present
    if web_fallback_attempted and not usable_web_present:
        with stage(timing, "tool:web_search", "external source fallback"):
            fallback_call = asdict(external.web_search(question, topic="general"))
        fallback_call["queried_at_utc"] = datetime.now(timezone.utc).isoformat()
        fallback_call["fallback_from_tools"] = list(failed_official_tools)
        calls.append(fallback_call)
        web_fallback_used = _usable_web_call(fallback_call)

    markdown_response = payload.get("markdown_response")
    projected_markdown = (
        dict(markdown_response)
        if isinstance(markdown_response, Mapping)
        else {
            "markdown": str(payload.get("answer") or ""),
            "fact_md": str(payload.get("answer") or ""),
            "data_md": "",
            "notice_md": "",
        }
    )
    projected_markdown["evidence"] = []
    projected_markdown["verification"] = {
        "status": (
            "pass"
            if any(external_call_has_usable_result(call) for call in calls)
            else "partial"
        )
    }
    sources = _source_names(calls)
    diagnostics = dict(payload.get("router_diagnostics") or {})
    diagnostics["external_passthrough"] = {
        "enabled": True,
        "web_fallback_attempted": web_fallback_attempted,
        "web_fallback_used": web_fallback_used,
        "failed_official_tools": list(failed_official_tools),
    }
    return {
        **payload,
        "tool_calls": calls,
        "sources": sources,
        "markdown_response": projected_markdown,
        "router_diagnostics": diagnostics,
        EXTERNAL_PASSTHROUGH_FIELD: {
            "enabled": True,
            "queried_at_utc": observed_at,
            "web_fallback_attempted": web_fallback_attempted,
            "web_fallback_used": web_fallback_used,
            "failed_official_tools": list(failed_official_tools),
        },
    }


def is_external_passthrough_result(result: Mapping[str, object]) -> bool:
    marker = result.get(EXTERNAL_PASSTHROUGH_FIELD)
    return isinstance(marker, Mapping) and marker.get("enabled") is True


def external_passthrough_calls(result: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    raw_calls = result.get("tool_calls")
    if not isinstance(raw_calls, list):
        return ()
    return tuple(
        dict(call)
        for call in raw_calls
        if isinstance(call, Mapping) and is_external_passthrough_tool(call.get("tool"))
    )


def _is_official_tool(call: Mapping[str, object]) -> bool:
    tool = str(call.get("tool") or "").strip().casefold()
    return tool.startswith(_OFFICIAL_TOOL_PREFIXES)


def _diagnostic_external_tools(payload: Mapping[str, object]) -> tuple[str, ...]:
    diagnostics = payload.get("router_diagnostics")
    if not isinstance(diagnostics, Mapping):
        return ()
    found: list[str] = []

    def visit(value: object, key: str = "") -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, str(child_key).casefold())
            return
        if isinstance(value, list):
            for child in value:
                visit(child, key)
            return
        if key not in {"tool", "tool_name", "eligible_tools"}:
            return
        tool = str(value or "").strip()
        if tool.casefold().startswith(_OFFICIAL_TOOL_PREFIXES):
            found.append(tool)

    visit(diagnostics)
    return tuple(dict.fromkeys(found))


def _payload_failed(payload: Mapping[str, object]) -> bool:
    metrics = payload.get("agent_loop_metrics")
    if isinstance(metrics, Mapping):
        status = str(metrics.get("status") or "").strip().casefold()
        if status not in {"", "ok", "partial", "pass"}:
            return True
    decomposition = payload.get("decomposition")
    if not isinstance(decomposition, list):
        return False
    return any(
        isinstance(item, Mapping)
        and str(item.get("status") or "").strip().casefold()
        not in {"", "ok", "partial", "pass"}
        for item in decomposition
    )


def _call_needs_web_fallback(call: Mapping[str, object]) -> bool:
    status = str(call.get("status") or "").strip().casefold()
    if status in _FAILED_STATUSES:
        return True
    render_data = call.get("render_data")
    if not isinstance(render_data, Mapping):
        return status not in {"fixture", "live", "ok", "partial"}
    error_code = str(render_data.get("error_code") or "").strip()
    return bool(error_code)


def external_call_has_usable_result(call: Mapping[str, object]) -> bool:
    status = str(call.get("status") or "").strip().casefold()
    if status in _FAILED_STATUSES:
        return False
    render_data = call.get("render_data")
    if not isinstance(render_data, Mapping):
        return bool(str(call.get("summary_text") or "").strip())
    if isinstance(render_data.get("items"), list):
        return any(isinstance(item, Mapping) for item in render_data["items"])
    ignored = {"error_code", "error_message", "query", "request", "status"}
    return any(value not in (None, "", [], {}) for key, value in render_data.items() if key not in ignored)


def _usable_web_call(call: Mapping[str, object]) -> bool:
    return (
        str(call.get("tool") or "").casefold() == "web_search"
        and external_call_has_usable_result(call)
    )


def _source_names(calls: Sequence[Mapping[str, object]]) -> list[str]:
    return list(
        dict.fromkeys(
            str(call.get("source") or call.get("tool") or "external")
            for call in calls
            if is_external_passthrough_tool(call.get("tool"))
        )
    )
