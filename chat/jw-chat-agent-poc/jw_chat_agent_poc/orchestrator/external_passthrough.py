from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import date, datetime, timezone
import json
import re
from typing import Any, Final
from urllib.parse import urlparse

from jw_chat_agent_poc.common.timing import Timing, stage
from jw_chat_agent_poc.orchestrator.source_grading import SourceGrade, grade_web_url
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
_RECENT_WEB_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:최근|최신|개정|변경|뉴스|이슈)",
    re.IGNORECASE,
)
_FRESHNESS_REQUIRED_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:(?:최근|최신).{0,16}(?:개정|변경|고시|기준|내용)|"
    r"(?:개정|변경|고시|기준).{0,16}(?:최근|최신))",
    re.IGNORECASE,
)
_WEB_STAT_METRIC_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:유병률|환자\s*수|발생률|유병\s*환자|진료\s*인원|질환\s*환자)",
    re.IGNORECASE,
)
_WEB_STAT_VALUE_RE: Final[re.Pattern[str]] = re.compile(
    r"\d[\d,.]*\s*(?:%|명|건|만\s*명|억\s*명)",
    re.IGNORECASE,
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
_SOURCE_GRADE_LABELS: Final[dict[SourceGrade, str]] = {
    SourceGrade.AUTHORITATIVE: "A 공식",
    SourceGrade.SUPPLEMENTARY: "B 기관·학술",
    SourceGrade.UNVERIFIED: "C 기타·개인",
}
_INSTITUTION_BY_HOST: Final[dict[str, str]] = {
    "hira.or.kr": "건강보험심사평가원",
    "mfds.go.kr": "식품의약품안전처",
    "clinicaltrials.gov": "ClinicalTrials.gov",
    "snuh.org": "서울대학교병원",
    "snubh.org": "분당서울대학교병원",
    "amc.seoul.kr": "서울아산병원",
    "stcarollo.or.kr": "성가롤로병원",
}
_POPULATION_CONTEXT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:(?:조사|연구|분석|등록)\s*(?:대상|표본|참여자)?[^\d]{0,16}"
    r"\d[\d,]*(?:\.\d+)?\s*명|\d[\d,]*(?:\.\d+)?\s*명을\s*대상)",
    re.IGNORECASE,
)
_SURVEY_YEAR_CONTEXT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:(20\d{2})년?[^\n]{0,24}(?:조사|연구|자료|분석)|"
    r"(?:조사|연구|자료|분석)[^\n]{0,24}(20\d{2})년?)",
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
    core_web_present = any(_web_call_answers_question(question, call) for call in calls)
    failed_web_search = any(
        str(call.get("tool") or "").strip().casefold() == "web_search"
        for call in calls
    ) and not core_web_present
    identity_mismatch = any(_call_has_identity_mismatch(call) for call in calls)
    web_fallback_attempted = (
        bool(failed_official_tools) or failed_web_search
    ) and not _exact_hira_patient_count_question(question) and not identity_mismatch
    web_fallback_used = web_fallback_attempted and usable_web_present
    fallback_queries: list[str] = []
    if web_fallback_attempted and not core_web_present:
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
            "partial"
            if any(external_result_status(call) != "PARTIAL" for call in calls)
            else "pass"
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
    status_diagnostics = _external_status_diagnostics(calls)
    diagnostics = dict(payload.get("router_diagnostics") or {})
    diagnostics["external_passthrough"] = {
        "enabled": True,
        "web_fallback_attempted": web_fallback_attempted,
        "web_fallback_used": web_fallback_used,
        "failed_official_tools": list(failed_official_tools),
        "fallback_queries": fallback_queries,
        **status_diagnostics,
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
            **status_diagnostics,
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
    if any(
        str(call.get("tool") or "").startswith("hira_disease_")
        and isinstance(call.get("render_data"), Mapping)
        and call["render_data"].get("direct_code_lookup") is True
        for call in calls
    ):
        return False
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
    if any(_web_call_answers_question(question, call) for call in calls):
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
    status_diagnostics = _external_status_diagnostics(calls)
    marker.update(
        {
            "enabled": True,
            "web_fallback_attempted": True,
            "web_fallback_used": web_fallback_used,
            "fallback_reason": reason,
            "fallback_queries": fallback_queries,
            **status_diagnostics,
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
            **status_diagnostics,
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


def _exact_hira_patient_count_question(question: str) -> bool:
    return bool(re.search(r"환자\s*수|입원|외래", question)) and "유병률" not in question


def _external_status_diagnostics(
    calls: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    statuses = tuple(external_result_status(call) for call in calls)
    failed = tuple(
        str(call.get("tool") or "unknown")
        for call, status in zip(calls, statuses, strict=True)
        if status in {"HARD_FAIL", "EMPTY", "SEMANTIC_EMPTY"}
    )
    usable = sum(status == "PARTIAL" for status in statuses)
    if not calls:
        external_status = "EMPTY"
    elif not failed:
        external_status = "VERIFIED"
    elif usable:
        external_status = "PARTIAL"
    else:
        external_status = statuses[0] if len(set(statuses)) == 1 else "HARD_FAIL"
    reasons = tuple(
        dict.fromkeys(
            str(call.get("render_data", {}).get("error") or call.get("summary_text") or status)
            for call, status in zip(calls, statuses, strict=True)
            if status in {"HARD_FAIL", "EMPTY", "SEMANTIC_EMPTY"}
            and isinstance(call.get("render_data"), Mapping)
        )
    )
    return {
        "external_status": external_status,
        "failed_dimensions": list(dict.fromkeys(failed)),
        "failure_reason": "; ".join(reasons),
    }


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


def _call_has_identity_mismatch(call: Mapping[str, object]) -> bool:
    render_data = call.get("render_data")
    if not isinstance(render_data, Mapping):
        return False
    return (
        str(render_data.get("error_code") or "").strip().upper() == "IDENTITY_MISMATCH"
        or str(render_data.get("blocked_reason") or "").strip().casefold() == "identity_mismatch"
    )


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


def _web_call_answers_question(question: str, call: Mapping[str, object]) -> bool:
    if not _usable_web_call(call):
        return False
    render_data = call.get("render_data")
    items = render_data.get("items") if isinstance(render_data, Mapping) else None
    if not isinstance(items, list):
        return False
    subject_match = _DISEASE_STAT_SUBJECT_RE.match(question) or _DISEASE_PATIENT_SUBJECT_RE.match(question)
    if subject_match is not None:
        subject = subject_match.group("subject").strip()
        for item in items:
            if not isinstance(item, Mapping):
                continue
            if _item_source_grade(item) is SourceGrade.UNVERIFIED:
                continue
            blob = _web_item_body(item)
            if (
                _disease_subject_relevant(subject, blob)
                and _WEB_STAT_METRIC_RE.search(blob)
                and _WEB_STAT_VALUE_RE.search(blob)
            ):
                return True
        return False
    if _FRESHNESS_REQUIRED_RE.search(question):
        return any(
            isinstance(item, Mapping)
            and _parse_publication_date(
                str(item.get("published_at") or item.get("published_date") or "")
            )
            is not None
            for item in items
        )
    return bool(items)


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
        topic = "news" if _RECENT_WEB_RE.search(question) else "general"
        with stage(timing, "tool:web_search", "external source fallback"):
            fallback_call = asdict(external.web_search(query, topic=topic))
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
        if _web_call_answers_question(question, fallback_call):
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
            (
                "institution_academic",
                f"site:mohw.go.kr OR site:or.kr OR site:ac.kr {subject} "
                "국내 유병률 환자수 통계 발생률 역학 연구 prevalence Korea",
            ),
            ("specialist_press", f"site:medicaltimes.com OR site:docdocdoc.co.kr {subject} 유병률 환자수 통계 발생률"),
        )
    if any(token in question for token in ("급여기준", "급여 기준", "급여조건", "급여 조건")):
        return (
            ("official_domain", f"site:hira.or.kr {question} 보험급여 인정기준 세부 조건 본문"),
            ("institution_academic", f"site:mohw.go.kr OR site:nhis.or.kr OR site:ac.kr {question} 개정 고시 급여 적용 조건"),
            ("specialist_press", f"site:yakup.com OR site:dailypharm.com OR site:medipana.com {question} 개정 급여 적용 조건"),
        )
    if _RECENT_WEB_RE.search(question):
        return (
            ("official_domain", f"site:jw-pharma.co.kr OR site:mfds.go.kr {question}"),
            ("institution_academic", f"site:go.kr OR site:or.kr OR site:ac.kr {question}"),
            ("specialist_press", f"site:dailypharm.com OR site:medicaltimes.com OR site:monews.co.kr {question}"),
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
    recent_revision = bool(_FRESHNESS_REQUIRED_RE.search(question))
    disease_stat_question = bool(
        _DISEASE_STAT_SUBJECT_RE.match(question) or _DISEASE_PATIENT_SUBJECT_RE.match(question)
    )
    subject_match = _DISEASE_STAT_SUBJECT_RE.match(question) or _DISEASE_PATIENT_SUBJECT_RE.match(question)
    disease_subject = subject_match.group("subject").strip() if subject_match else ""
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
        grade = grade_web_url(url)
        item["source_grade"] = _SOURCE_GRADE_LABELS[grade]
        item["institution_name"] = _institution_name(host, item)
        item["population_text"] = _population_text(item)
        item["survey_year"] = _survey_year(item)
        if host in _PERSONAL_BLOG_HOSTS or any(host.endswith(f".{name}") for name in _PERSONAL_BLOG_HOSTS):
            rejected.append({"url": url, "reason": "personal_blog"})
            continue
        if disease_stat_question:
            if grade is SourceGrade.UNVERIFIED:
                rejected.append({"url": url, "reason": "authority_required"})
                continue
            if host in {"youtube.com", "www.youtube.com", "youtu.be"}:
                rejected.append({"url": url, "reason": "video_not_quantitative_evidence"})
                continue
            body = _web_item_body(item)
            if not _substantive_web_body(body):
                rejected.append({"url": url, "reason": "body_extraction_failed"})
                continue
            if not (_WEB_STAT_METRIC_RE.search(body) and _WEB_STAT_VALUE_RE.search(body)):
                rejected.append({"url": url, "reason": "requested_statistic_absent_from_body"})
                continue
            if not _disease_subject_relevant(disease_subject, body):
                rejected.append({"url": url, "reason": "disease_relevance_mismatch"})
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


def _web_item_body(item: Mapping[str, object]) -> str:
    return " ".join(
        str(item.get(key) or "").strip()
        for key in ("content", "snippet", "raw_content", "text")
        if str(item.get(key) or "").strip()
    )


def _substantive_web_body(body: str) -> bool:
    normalized = re.sub(r"\s+", " ", body).strip()
    if len(normalized) < 24:
        return False
    return normalized.casefold() not in {"로그인", "login", "sign in"}


def _disease_subject_relevant(subject: str, body: str) -> bool:
    normalize = lambda value: re.sub(r"[^0-9a-z가-힣]", "", value.casefold()).replace("성", "")
    normalized_subject = normalize(subject)
    normalized_body = normalize(body)
    if normalized_subject and normalized_subject in normalized_body:
        return True
    medical_tokens = tuple(
        token
        for token in ("당뇨", "망막", "황반", "혈소판", "자반", "뇌경색", "유병")
        if token in normalized_subject
    )
    return len(medical_tokens) >= 2 and all(token in normalized_body for token in medical_tokens)


def _item_source_grade(item: Mapping[str, object]) -> SourceGrade:
    label = str(item.get("source_grade") or "").strip()
    if label.startswith("A "):
        return SourceGrade.AUTHORITATIVE
    if label.startswith("B "):
        return SourceGrade.SUPPLEMENTARY
    return grade_web_url(str(item.get("url") or ""))


def _institution_name(host: str, item: Mapping[str, object]) -> str:
    for domain, name in _INSTITUTION_BY_HOST.items():
        if host == domain or host.endswith(f".{domain}"):
            return name
    supplied = str(item.get("institution") or item.get("source_name") or "").strip()
    return supplied or host or "확인 불가"


def _population_text(item: Mapping[str, object]) -> str:
    supplied = str(item.get("population") or item.get("population_text") or "").strip()
    if supplied:
        return supplied
    blob = " ".join(str(item.get(key) or "") for key in ("title", "snippet", "content"))
    match = _POPULATION_CONTEXT_RE.search(blob)
    return match.group(0).strip() if match is not None else "확인 불가"


def _survey_year(item: Mapping[str, object]) -> str:
    supplied = str(item.get("survey_year") or item.get("survey_years") or "").strip()
    if supplied:
        return supplied
    blob = " ".join(str(item.get(key) or "") for key in ("title", "snippet", "content"))
    match = _SURVEY_YEAR_CONTEXT_RE.search(blob)
    if match is None:
        return "확인 불가"
    return next(group for group in match.groups() if group)


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
