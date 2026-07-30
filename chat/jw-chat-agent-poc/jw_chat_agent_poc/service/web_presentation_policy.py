from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from jw_chat_agent_poc.orchestrator.source_grading import (
    SourceGrade,
    grade_evidence_source,
    is_official_web_url,
    is_web_search_call,
    requested_authority_source_explicit,
)
from jw_chat_agent_poc.tool_use.routing_v4_execution import (
    official_web_fallback_decision_from_calls,
)


_EXPLICIT_WEB_RE: Final = re.compile(
    r"(?:웹|인터넷|뉴스|기사|보도|최근\s*이슈|최신\s*이슈|동향|교차\s*검증)",
    re.IGNORECASE,
)
_UPSTREAM_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "CONNECTION_ERROR",
        "HTTP_ERROR",
        "RATE_LIMITED",
        "SERVER_ERROR",
        "SERVICE_UNAVAILABLE",
        "TIMEOUT",
        "TOOL_TIMEOUT",
        "UPSTREAM_UNAVAILABLE",
    }
)
_NON_FALLBACK_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "AMBIGUOUS_INPUT",
        "CAPABILITY_NOT_IMPLEMENTED",
        "INVALID_INPUT",
        "INVALID_TOOL_ARGUMENTS",
        "NO_MATCH",
        "SCHEMA_INVALID",
    }
)
_UPSTREAM_TEXT_RE: Final = re.compile(
    r"(?:timeout|timed\s*out|upstream|connection|service\s*unavailable|HTTP\s*50[23]|시간\s*초과|서버\s*장애)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class WebPresentationDecision:
    show_all_results: bool
    accepted_urls: tuple[str, ...]
    disclosure: str
    reason_code: str


def web_presentation_policy(
    question: str,
    tool_calls: Sequence[Mapping[str, object]],
) -> WebPresentationDecision:
    web_calls = tuple(call for call in tool_calls if is_web_search_call(call))
    if not web_calls:
        return _deny("NO_WEB_RESULT")
    canonical_decision = official_web_fallback_decision_from_calls(web_calls)
    if canonical_decision is not None:
        return WebPresentationDecision(
            show_all_results=False,
            accepted_urls=canonical_decision.accepted_urls,
            disclosure=canonical_decision.disclosure,
            reason_code=canonical_decision.reason_code,
        )
    if any(
        requested_authority_source_explicit(question, source_domain=domain)
        for domain in ("hira", "regulatory", "clinical_trials")
    ):
        return _deny("EXPLICIT_SOURCE_NO_FALLBACK")
    if _EXPLICIT_WEB_RE.search(question):
        return WebPresentationDecision(
            show_all_results=True,
            accepted_urls=(),
            disclosure="",
            reason_code="EXPLICIT_WEB_REQUEST",
        )

    authority_calls = tuple(call for call in tool_calls if _is_authority_call(call))
    if any(_has_usable_result(call) for call in authority_calls):
        return _deny("PARTIAL_RESULT")
    if not authority_calls:
        return _deny("NO_AUTHORITY_ROUTE")
    if not all(_is_upstream_failure(call) for call in authority_calls):
        return _deny(_non_fallback_reason(authority_calls))

    domains = tuple(dict.fromkeys(filter(None, (_source_domain(call) for call in authority_calls))))
    if not domains:
        return _deny("NO_OFFICIAL_WEB_DOMAIN")
    accepted_urls = tuple(
        dict.fromkeys(
            url
            for call in web_calls
            for url in _web_urls(call)
            if any(is_official_web_url(url, source_domain=domain) for domain in domains)
        )
    )
    if not accepted_urls:
        return _deny("NO_ALLOWLISTED_WEB_RESULT")
    return WebPresentationDecision(
        show_all_results=False,
        accepted_urls=accepted_urls,
        disclosure=_upstream_disclosure(domains),
        reason_code="UPSTREAM_UNAVAILABLE",
    )


def _deny(reason_code: str) -> WebPresentationDecision:
    return WebPresentationDecision(
        show_all_results=False,
        accepted_urls=(),
        disclosure="",
        reason_code=reason_code,
    )


def _is_authority_call(call: Mapping[str, object]) -> bool:
    if is_web_search_call(call):
        return False
    grade = grade_evidence_source(
        tool=str(call.get("tool") or ""),
        source=str(call.get("source") or ""),
    )
    return grade is SourceGrade.AUTHORITATIVE


def _has_usable_result(call: Mapping[str, object]) -> bool:
    if str(call.get("status") or "").strip().lower() not in {"ok", "partial"}:
        return False
    data = call.get("render_data")
    if not isinstance(data, Mapping):
        return False
    ignored = {"error", "error_code", "message", "provider", "request", "status"}
    return any(key not in ignored and value not in (None, "", [], {}) for key, value in data.items())


def _is_upstream_failure(call: Mapping[str, object]) -> bool:
    if str(call.get("status") or "").strip().lower() in {"ok", "partial"}:
        return False
    data = call.get("render_data")
    error_code = ""
    error_text = str(call.get("summary_text") or "")
    if isinstance(data, Mapping):
        error_code = str(data.get("error_code") or "").strip().upper()
        error_text = " ".join(
            (
                error_text,
                str(data.get("error") or ""),
                str(data.get("message") or ""),
            )
        )
    if error_code in _NON_FALLBACK_ERROR_CODES:
        return False
    if error_code in _UPSTREAM_ERROR_CODES:
        return True
    return bool(_UPSTREAM_TEXT_RE.search(error_text))


def _non_fallback_reason(calls: Sequence[Mapping[str, object]]) -> str:
    for call in calls:
        data = call.get("render_data")
        if not isinstance(data, Mapping):
            continue
        error_code = str(data.get("error_code") or "").strip().upper()
        if error_code in _NON_FALLBACK_ERROR_CODES:
            return error_code
    return "NON_FALLBACK_FAILURE"


def _source_domain(call: Mapping[str, object]) -> str:
    identity = f"{call.get('tool') or ''} {call.get('source') or ''}".lower()
    if "hira" in identity or "심평원" in identity or "건강보험심사평가원" in identity:
        return "hira"
    if "mfds" in identity or "식약처" in identity or "식품의약품안전처" in identity:
        return "regulatory"
    if "clinicaltrials" in identity or "cris" in identity:
        return "clinical_trials"
    return ""


def _web_urls(call: Mapping[str, object]) -> tuple[str, ...]:
    data = call.get("render_data")
    if not isinstance(data, Mapping):
        return ()
    urls: list[str] = []
    direct = data.get("items")
    if isinstance(direct, list):
        urls.extend(
            str(item.get("url") or "").strip()
            for item in direct
            if isinstance(item, Mapping)
        )
    nested = data.get("calls")
    if isinstance(nested, list):
        for item in nested:
            if isinstance(item, Mapping):
                urls.extend(_web_urls(item))
    return tuple(url for url in urls if url)


def _upstream_disclosure(domains: tuple[str, ...]) -> str:
    labels = {
        "hira": "HIRA 공식 통계",
        "regulatory": "식품의약품안전처 공식 정보",
        "clinical_trials": "공식 임상시험 정보",
    }
    source_text = "·".join(labels[domain] for domain in domains)
    return (
        f"{source_text} 조회에 실패했습니다(UPSTREAM_UNAVAILABLE). "
        "아래는 공식 도메인의 웹 검색 결과이며 공식 통계가 아닙니다. "
        "정확한 수치는 해당 공식 시스템에서 확인하십시오."
    )
