from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import date, datetime, timezone
import json
import re
from typing import Any, Final
from urllib.parse import urlparse

from jw_chat_agent_poc.common.timing import Timing, stage
from jw_chat_agent_poc.tools.external import ExternalApiClient


EXTERNAL_PASSTHROUGH_FIELD: Final = "_external_passthrough"
WEB_FALLBACK_DISCLOSURE: Final = "공식 소스에서 확인하지 못해 웹 검색 결과로 답합니다"
RESULT_STATUS_FIELD: Final = "result_status"

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
        "timeout",
        "unavailable",
        "unsupported",
    }
)
_EMPTY_STATUSES: Final[frozenset[str]] = frozenset(
    {"empty", "inapplicable", "missing_key", "no_data", "unavailable", "unsupported"}
)
_PERSONAL_BLOG_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "blog.naver.com",
        "m.blog.naver.com",
        "brunch.co.kr",
        "medium.com",
        "tistory.com",
    }
)
_RECENT_REVISION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:최근|최신|개정|변경).{0,20}(?:급여|고시|기준|내용)|"
    r"(?:급여|고시|기준).{0,20}(?:최근|최신|개정|변경)"
)
_INTERNAL_STRUCTURED_QUESTION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:\bUBIST\b|\bIQVIA\b|\bCSD\b|\bHHI\b|시장\s*규모|시장\s*점유|점유율|"
    r"매출|처방|시장\s*순위|브랜드\s*순위|경쟁\s*구도|채널|진료과|"
    r"market\s*share|sales)",
    re.IGNORECASE,
)
_DISEASE_PATIENT_SUBJECT_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?P<subject>.+?)\s*(?:상병\s*)?(?:환자\s*수|환자수)(?:\s.*)?$",
    re.IGNORECASE,
)
_DISEASE_STAT_SUBJECT_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?P<subject>.+?)(?:\s+국내)?\s*(?:유병률|환자\s*수|발생률|통계)(?:\s.*)?$",
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
    external: ExternalApiClient | None,
    timing: Timing | None = None,
) -> dict[str, Any]:
    if is_external_passthrough_result(payload):
        return payload
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
        {
            **call,
            "queried_at_utc": call.get("queried_at_utc") or observed_at,
            RESULT_STATUS_FIELD: external_result_status(call),
        }
        for call in calls
    ]
    for call in calls:
        if str(call.get("tool") or "").strip().casefold() == "web_search":
            _apply_web_result_policy(question, calls, call)
            call[RESULT_STATUS_FIELD] = external_result_status(call)
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
    usable_web_present = any(_usable_web_call(call) for call in calls)
    failed_web_search = any(
        str(call.get("tool") or "").strip().casefold() == "web_search"
        for call in calls
    ) and not usable_web_present
    web_fallback_attempted = bool(failed_official_tools) or failed_web_search
    web_fallback_used = web_fallback_attempted and usable_web_present
    fallback_queries: list[str] = []
    if web_fallback_attempted and not usable_web_present:
        if external is None:
            return payload
        fallback_from_tools = failed_official_tools or ("web_search",)
        fallback_reason = (
            "official_tool_failed_or_empty"
            if failed_official_tools
            else "web_search_failed_or_empty"
        )
        fallback_queries = _append_web_fallback_calls(
            question,
            calls,
            external=external,
            timing=timing,
            fallback_from_tools=fallback_from_tools,
            reason=fallback_reason,
            max_attempts=1 if failed_web_search and not failed_official_tools else None,
        )
        web_fallback_used = any(_usable_web_call(call) for call in calls)

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
    existing_sources = payload.get("sources")
    prior_sources = (
        [str(source) for source in existing_sources if source]
        if isinstance(existing_sources, list | tuple)
        else []
    )
    sources = list(
        dict.fromkeys(
            [*prior_sources, *_source_names(calls)]
        )
    )
    diagnostics = dict(payload.get("router_diagnostics") or {})
    diagnostics["external_passthrough"] = {
        "enabled": True,
        "web_fallback_attempted": web_fallback_attempted,
        "web_fallback_used": web_fallback_used,
        "failed_official_tools": list(failed_official_tools),
        "fallback_queries": fallback_queries,
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
            "fallback_queries": fallback_queries,
        },
    }


def prepare_existing_external_passthrough(
    question: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Mark external calls added after agent execution without issuing new I/O."""
    return prepare_external_passthrough(question, payload, external=None)


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


def external_passthrough_needs_core_assessment(result: Mapping[str, object]) -> bool:
    marker = result.get(EXTERNAL_PASSTHROUGH_FIELD)
    if not isinstance(marker, Mapping) or marker.get("web_fallback_used") is True:
        return False
    calls = external_passthrough_calls(result)
    return not any(_usable_web_call(call) for call in calls) and any(
        _is_official_tool(call) and external_call_has_usable_result(call)
        for call in calls
    )


def append_external_web_fallback(
    question: str,
    payload: dict[str, Any],
    *,
    external: ExternalApiClient,
    timing: Timing | None = None,
    reason: str,
) -> dict[str, Any]:
    raw_calls = payload.get("tool_calls")
    calls = [dict(call) for call in raw_calls if isinstance(call, Mapping)] if isinstance(raw_calls, list) else []
    if any(_usable_web_call(call) for call in calls):
        return payload
    official_tools = tuple(
        dict.fromkeys(str(call.get("tool") or "") for call in calls if _is_official_tool(call))
    )
    fallback_queries = _append_web_fallback_calls(
        question,
        calls,
        external=external,
        timing=timing,
        fallback_from_tools=official_tools,
        reason=reason,
    )
    web_fallback_used = any(_usable_web_call(call) for call in calls)
    marker = dict(payload.get(EXTERNAL_PASSTHROUGH_FIELD) or {})
    marker.update(
        {
            "enabled": True,
            "web_fallback_attempted": True,
            "web_fallback_used": web_fallback_used,
            "fallback_reason": reason,
            "fallback_queries": fallback_queries,
        }
    )
    diagnostics = dict(payload.get("router_diagnostics") or {})
    passthrough_diagnostics = dict(diagnostics.get("external_passthrough") or {})
    passthrough_diagnostics.update(
        {
            "enabled": True,
            "web_fallback_attempted": True,
            "web_fallback_used": web_fallback_used,
            "fallback_reason": reason,
            "fallback_queries": fallback_queries,
        }
    )
    diagnostics["external_passthrough"] = passthrough_diagnostics
    existing_sources = payload.get("sources")
    prior_sources = (
        [str(source) for source in existing_sources if source]
        if isinstance(existing_sources, (list, tuple))
        else []
    )
    return {
        **payload,
        "tool_calls": calls,
        "sources": list(dict.fromkeys([*prior_sources, *_source_names(calls)])),
        "router_diagnostics": diagnostics,
        EXTERNAL_PASSTHROUGH_FIELD: marker,
    }


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
    return external_result_status(call) in {"HARD_FAIL", "EMPTY", "SEMANTIC_EMPTY"}


def external_result_status(call: Mapping[str, object]) -> str:
    """Classify the result content, separately from HTTP/tool transport success."""

    explicit = str(call.get(RESULT_STATUS_FIELD) or "").strip().upper()
    if explicit in {"HARD_FAIL", "EMPTY", "PARTIAL", "SEMANTIC_EMPTY"}:
        return explicit
    status = str(call.get("status") or "").strip().casefold()
    render_data = call.get("render_data")
    error_code = (
        str(render_data.get("error_code") or "").strip()
        if isinstance(render_data, Mapping)
        else ""
    )
    if status in _EMPTY_STATUSES:
        return "EMPTY"
    if status in _FAILED_STATUSES or error_code:
        return "HARD_FAIL"
    if _is_reimbursement_call(call):
        blob = json.dumps(render_data or {}, ensure_ascii=False, sort_keys=True, default=str)
        body_tokens = ("요양급여", "투여대상", "투여횟수", "제외기준", "인정기준 이외")
        shell_tokens = ("첨부파일", "파일명", "고시 제", "게시일", "notice_number")
        if any(token in blob for token in body_tokens):
            return "PARTIAL"
        if any(token in blob for token in shell_tokens):
            return "SEMANTIC_EMPTY"
    return "PARTIAL" if _raw_call_has_usable_result(call) else "EMPTY"


def external_call_has_usable_result(call: Mapping[str, object]) -> bool:
    if external_result_status(call) in {"HARD_FAIL", "EMPTY", "SEMANTIC_EMPTY"}:
        return False
    return _raw_call_has_usable_result(call)


def _raw_call_has_usable_result(call: Mapping[str, object]) -> bool:
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


def _append_web_fallback_calls(
    question: str,
    calls: list[dict[str, Any]],
    *,
    external: ExternalApiClient,
    timing: Timing | None,
    fallback_from_tools: Sequence[str],
    reason: str,
    max_attempts: int | None = None,
) -> list[str]:
    attempted: list[str] = []
    query_plan = _web_fallback_query_plan(question)
    if max_attempts is not None:
        query_plan = query_plan[:max_attempts]
    for index, (tier, query) in enumerate(query_plan, start=1):
        with stage(timing, "tool:web_search", "external source fallback"):
            fallback_call = asdict(external.web_search(query, topic="general"))
        fallback_call["queried_at_utc"] = datetime.now(timezone.utc).isoformat()
        fallback_call["fallback_from_tools"] = list(fallback_from_tools)
        fallback_call["fallback_reason"] = reason
        fallback_call["fallback_query"] = query
        fallback_call["fallback_attempt"] = index
        fallback_call["fallback_tier"] = tier
        _apply_web_result_policy(question, calls, fallback_call)
        fallback_call[RESULT_STATUS_FIELD] = external_result_status(fallback_call)
        calls.append(fallback_call)
        attempted.append(query)
        if _usable_web_call(fallback_call):
            break
    return attempted


def _web_fallback_queries(question: str) -> tuple[str, ...]:
    return tuple(query for _, query in _web_fallback_query_plan(question))


def _web_fallback_query_plan(question: str) -> tuple[tuple[str, str], ...]:
    disease_stat_match = _DISEASE_STAT_SUBJECT_RE.match(question)
    patient_match = _DISEASE_PATIENT_SUBJECT_RE.match(question)
    subject_match = disease_stat_match or patient_match
    if subject_match is not None:
        subject = subject_match.group("subject").strip()
        subject = re.sub(r"^국내\s+|\s+국내$", "", subject).strip()
        return (
            ("official_domain", f"site:hira.or.kr OR site:nhis.or.kr {subject} 국내 유병률 환자수 통계 발생률"),
            ("institution_academic", f"site:mohw.go.kr OR site:ac.kr {subject} 유병률 환자수 통계 발생률"),
            ("specialist_press", f"site:medicaltimes.com OR site:docdocdoc.co.kr {subject} 유병률 환자수 통계 발생률"),
        )
    if any(token in question for token in ("급여기준", "급여 기준", "급여조건", "급여 조건")):
        return (
            ("official_domain", f"site:hira.or.kr {question} 보험급여 인정기준 세부 조건 본문"),
            ("institution_academic", f"site:mohw.go.kr OR site:nhis.or.kr OR site:ac.kr {question} 개정 고시 급여 적용 조건"),
            ("specialist_press", f"site:yakup.com OR site:dailypharm.com OR site:medipana.com {question} 개정 급여 적용 조건"),
        )
    return (
        ("official_domain", f"site:hira.or.kr OR site:mfds.go.kr OR site:clinicaltrials.gov {question}"),
        ("institution_academic", f"site:go.kr OR site:or.kr OR site:ac.kr {question}"),
        ("specialist_press", question),
    )


def _is_reimbursement_call(call: Mapping[str, object]) -> bool:
    return "reimbursement" in str(call.get("tool") or "").casefold()


def _apply_web_result_policy(
    question: str,
    existing_calls: Sequence[Mapping[str, object]],
    fallback_call: dict[str, Any],
) -> None:
    render_data = fallback_call.get("render_data")
    if not isinstance(render_data, dict):
        return
    items = render_data.get("items")
    if not isinstance(items, list):
        return
    recent_revision = bool(_RECENT_REVISION_RE.search(question))
    official_date = _official_reference_date(existing_calls)
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for raw_item in items:
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        published_at = str(item.get("published_at") or item.get("published_date") or "").strip()
        item["published_at"] = published_at or None
        url = str(item.get("url") or "")
        host = (urlparse(url).hostname or "").casefold()
        if host in _PERSONAL_BLOG_HOSTS or any(host.endswith(f".{name}") for name in _PERSONAL_BLOG_HOSTS):
            rejected.append({"url": url, "reason": "personal_blog"})
            continue
        published_date = _parse_publication_date(published_at)
        if recent_revision and published_date is None:
            rejected.append({"url": url, "reason": "published_at_required"})
            continue
        if recent_revision and official_date is not None and published_date is not None and published_date < official_date:
            rejected.append({"url": url, "reason": "published_before_official_effective_date"})
            continue
        kept.append(item)
    render_data["items"] = kept
    render_data["rejected_items"] = rejected
    render_data["official_reference_date"] = official_date.isoformat() if official_date else None
    render_data["source_precedence"] = "official_wins_web_conflict_disclosed_only"
    fallback_call["status"] = "live" if kept else "no_data"


def _official_reference_date(calls: Sequence[Mapping[str, object]]) -> date | None:
    found: list[date] = []

    def visit(value: object, key: str = "") -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, str(child_key).casefold())
            return
        if isinstance(value, list):
            for child in value:
                visit(child, key)
            return
        if key not in {
            "effective_date",
            "notice_date",
            "published_at",
            "published_date",
            "source_date",
        }:
            return
        parsed = _parse_publication_date(str(value or ""))
        if parsed is not None:
            found.append(parsed)

    for call in calls:
        if _is_official_tool(call):
            visit(call)
    return max(found) if found else None


def _parse_publication_date(value: str) -> date | None:
    match = re.search(r"(?P<year>20\d{2})[-./](?P<month>\d{1,2})[-./](?P<day>\d{1,2})", value)
    if match is None:
        return None
    try:
        return date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
    except ValueError:
        return None


def _source_names(calls: Sequence[Mapping[str, object]]) -> list[str]:
    return list(
        dict.fromkeys(
            str(call.get("source") or call.get("tool") or "external")
            for call in calls
            if is_external_passthrough_tool(call.get("tool"))
        )
    )
