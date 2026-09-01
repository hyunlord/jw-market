from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from jw_chat_agent_poc.orchestrator.source_grading import (
    is_official_web_url,
    official_web_domains,
)
from jw_chat_agent_poc.tool_use.contracts import AgentResult, EvidenceFact
from jw_chat_agent_poc.tool_use.renderer import render_evidence_claim
from jw_chat_agent_poc.tool_use.routing_v4_capabilities import verify_claim_evidence
from jw_chat_agent_poc.tool_use.routing_v4_plan_support import RoutePlan


OFFICIAL_WEB_FALLBACK_FLAG: Final = "CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED"
OFFICIAL_WEB_FALLBACK_DECISION_FIELD: Final = "_official_web_fallback_decision"
_WEB_FALLBACK_RUNTIME_REASONS: Final[frozenset[str]] = frozenset(
    {
        "UPSTREAM_UNAVAILABLE",
        "NO_EVIDENCE",
        "NO_RECORD_FOUND",
        "PARTIAL_RESULT",
    }
)
_OFFICIAL_WEB_ENTRY_POINTS: Final[dict[str, tuple[str, str]]] = {
    "hira": (
        "HIRA 보험인정기준 검색",
        "https://www.hira.or.kr/rc/insu/insuadtcrtr/InsuAdtCrtrList.do",
    ),
    "regulatory": (
        "식품의약품안전처 의약품 검색",
        "https://nedrug.mfds.go.kr/",
    ),
}
_REQUEST_ENDING_RE: Final[re.Pattern[str]] = re.compile(
    r"\s*(?:에\s*대해(?:서)?\s*)?(?:알려\s*줘|알려\s*주세요|확인해\s*줘|확인해\s*주세요)[?.!]*$"
)
_IDENTITY_SEARCH_TERM_RE: Final[re.Pattern[str]] = re.compile(
    r"\s*(?:의\s*)?(?:급여\s*기준|급여기준).*$"
)


@dataclass(frozen=True, slots=True)
class OfficialWebFallbackDecision:
    web_call_budget: int
    accepted_urls: tuple[str, ...]
    separate_section: bool
    reason_code: str
    disclosure: str


def official_web_fallback_decision_payload(
    decision: OfficialWebFallbackDecision,
) -> dict[str, object]:
    return {
        "web_call_budget": decision.web_call_budget,
        "accepted_urls": list(decision.accepted_urls),
        "separate_section": decision.separate_section,
        "reason_code": decision.reason_code,
        "disclosure": decision.disclosure,
    }


def official_web_fallback_decision_from_calls(
    tool_calls: Sequence[Mapping[str, object]],
) -> OfficialWebFallbackDecision | None:
    for call in tool_calls:
        render_data = call.get("render_data")
        if not isinstance(render_data, Mapping):
            continue
        raw_decision = render_data.get(OFFICIAL_WEB_FALLBACK_DECISION_FIELD)
        if not isinstance(raw_decision, Mapping):
            continue
        accepted_urls = raw_decision.get("accepted_urls")
        if not isinstance(accepted_urls, list) or not all(
            isinstance(url, str) for url in accepted_urls
        ):
            continue
        web_call_budget = raw_decision.get("web_call_budget")
        separate_section = raw_decision.get("separate_section")
        reason_code = raw_decision.get("reason_code")
        disclosure = raw_decision.get("disclosure")
        if (
            type(web_call_budget) is not int
            or web_call_budget != 1
            or separate_section is not True
            or not accepted_urls
            or not isinstance(reason_code, str)
            or reason_code not in _WEB_FALLBACK_RUNTIME_REASONS
            or not isinstance(disclosure, str)
        ):
            continue
        return OfficialWebFallbackDecision(
            web_call_budget=web_call_budget,
            accepted_urls=tuple(accepted_urls),
            separate_section=separate_section,
            reason_code=reason_code,
            disclosure=disclosure,
        )
    return None


def actionable_official_web_failure(
    *,
    question: str,
    source_domain: str,
    reason_code: str,
    provider_outcome: str,
    internal_only: bool = False,
    authoritative_nonexistence_proven: bool = False,
) -> str | None:
    if internal_only or authoritative_nonexistence_proven:
        return None
    entry_point = _OFFICIAL_WEB_ENTRY_POINTS.get(source_domain)
    if entry_point is None:
        return None
    label, url = entry_point
    if not is_official_web_url(url, source_domain=source_domain):
        return None

    search_term = _action_search_term(
        question,
        identity_mismatch=reason_code == "IDENTITY_MISMATCH",
    )
    link = f"[{label}]({url})"
    if reason_code == "IDENTITY_MISMATCH":
        return (
            "연결된 고시의 제품 또는 성분 구성이 요청한 브랜드와 일치하지 않아 "
            "그 내용을 답으로 사용하지 않았습니다.\n"
            f"직접 확인: {link}\n"
            f"검색어: {search_term}\n"
            "정확한 제품명 또는 성분 구성을 확인해 다시 요청하면 해당 대상을 기준으로 조회합니다."
        )
    if provider_outcome == "empty":
        check_items = {
            "hira": "제품명, 성분 구성, 고시 시행일",
            "regulatory": "제품명, 성분명, 허가일",
        }[source_domain]
        return (
            "공식 웹 보완 검색을 시도했지만 허용된 공식 도메인에서 결과를 찾지 못했습니다.\n"
            f"직접 확인: {link}\n"
            f"검색어: {search_term}\n"
            f"확인할 항목: {check_items}"
        )
    if provider_outcome in {"timeout", "error"}:
        first_line = (
            "공식 웹 보완 검색이 5초 안에 완료되지 않았습니다."
            if provider_outcome == "timeout"
            else "공식 웹 보완 검색을 완료하지 못했습니다."
        )
        return (
            f"{first_line}\n"
            f"답을 추정하지 않고 공식 확인 경로를 안내합니다: {link}\n"
            f"검색어: {search_term}"
        )
    if provider_outcome == "unavailable":
        return (
            "현재 연결된 조회 도구에서는 요청 항목을 직접 제공하지 않습니다.\n"
            f"공식 확인 경로: {link}\n"
            f"검색어: {search_term}"
        )
    return None


def _action_search_term(question: str, *, identity_mismatch: bool) -> str:
    normalized = " ".join(question.split())
    normalized = _REQUEST_ENDING_RE.sub("", normalized).strip()
    if identity_mismatch:
        normalized = _IDENTITY_SEARCH_TERM_RE.sub("", normalized).strip()
    return normalized or "요청 대상 + 요청 항목"


def official_web_fallback_eligible(
    *,
    source_domain: str,
    runtime_reason: str,
    usable_authoritative_results: int,
    requested_source_explicit: bool,
    missing_requested_facets: tuple[str, ...] = (),
    internal_only: bool = False,
    authoritative_nonexistence_proven: bool = False,
) -> bool:
    del requested_source_explicit
    return (
        not internal_only
        and not authoritative_nonexistence_proven
        and runtime_reason in _WEB_FALLBACK_RUNTIME_REASONS
        and (
            runtime_reason == "PARTIAL_RESULT"
            and bool(missing_requested_facets)
            or runtime_reason != "PARTIAL_RESULT"
            and usable_authoritative_results == 0
        )
        and bool(official_web_domains(source_domain))
        and _official_web_fallback_enabled()
    )


_WEB_FALLBACK_FACET_TERMS = {
    "allocation": "배정 방식 allocation",
    "masking": "눈가림 masking",
    "intervention_model": "중재 모형 intervention model",
}


def official_web_fallback_query(
    question: str,
    *,
    source_domain: str,
    missing_requested_facets: tuple[str, ...] = (),
) -> str:
    domains = official_web_domains(source_domain)
    domain_clause = " OR ".join(f"site:{domain}" for domain in domains)
    facet_clause = " ".join(
        _WEB_FALLBACK_FACET_TERMS[facet]
        for facet in missing_requested_facets
        if facet in _WEB_FALLBACK_FACET_TERMS
    )
    query = " ".join(part for part in (question, facet_clause) if part)
    return f"{query} ({domain_clause})" if domain_clause else query


def official_web_fallback_policy(
    *,
    source_domain: str,
    runtime_reason: str,
    usable_authoritative_results: int,
    candidate_urls: tuple[str, ...],
    requested_source_explicit: bool = False,
    missing_requested_facets: tuple[str, ...] = (),
    internal_only: bool = False,
    authoritative_nonexistence_proven: bool = False,
) -> OfficialWebFallbackDecision:
    eligible = official_web_fallback_eligible(
        source_domain=source_domain,
        runtime_reason=runtime_reason,
        usable_authoritative_results=usable_authoritative_results,
        requested_source_explicit=requested_source_explicit,
        missing_requested_facets=missing_requested_facets,
        internal_only=internal_only,
        authoritative_nonexistence_proven=authoritative_nonexistence_proven,
    )
    if not eligible:
        if internal_only:
            reason_code = "INTERNAL_ONLY"
        elif authoritative_nonexistence_proven:
            reason_code = "PROVEN_NONEXISTENT"
        elif usable_authoritative_results > 0:
            reason_code = "PARTIAL_RESULT"
        else:
            reason_code = runtime_reason
        return OfficialWebFallbackDecision(
            web_call_budget=0,
            accepted_urls=(),
            separate_section=False,
            reason_code=reason_code,
            disclosure="",
        )

    accepted_urls = tuple(
        dict.fromkeys(
            url
            for url in candidate_urls
            if is_official_web_url(url, source_domain=source_domain)
        )
    )
    return OfficialWebFallbackDecision(
        web_call_budget=1 if accepted_urls else 0,
        accepted_urls=accepted_urls,
        separate_section=bool(accepted_urls),
        reason_code=runtime_reason,
        disclosure=(
            _official_web_disclosure(
                source_domain,
                requested_source_explicit=requested_source_explicit,
                runtime_reason=runtime_reason,
            )
            if accepted_urls
            else ""
        ),
    )


def _official_web_fallback_enabled() -> bool:
    return os.environ.get(OFFICIAL_WEB_FALLBACK_FLAG, "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def official_web_fallback_call_cap() -> int:
    return 1 if _official_web_fallback_enabled() else 0


def _official_web_disclosure(
    source_domain: str,
    *,
    requested_source_explicit: bool,
    runtime_reason: str,
) -> str:
    del runtime_reason
    source_name = {
        "hira": "HIRA 공식 통계",
        "regulatory": "식품의약품안전처 공식 정보",
        "clinical_trials": "공식 임상시험 정보",
    }.get(source_domain, "권위 원천")
    if requested_source_explicit:
        return (
            f"요청한 {source_name} 자료에서는 요청 범위를 확인하지 못했습니다. "
            f"아래 내용은 {source_name} 자료를 대신한 값이 아니라 공개 웹 보완 자료입니다."
        )
    return (
        f"{source_name}에서 요청 범위를 충분히 확인하지 못했습니다. "
        "아래는 공식 웹 보완 자료이며 내부 확인 자료와 구분됩니다. "
        "정확한 수치는 해당 공식 시스템에서 확인하십시오."
    )


def safe_execution_failure(result: AgentResult, *, reason_code: str) -> AgentResult:
    if result.answer.startswith("상태: 확인 불가"):
        answer = result.answer
    elif clinical_absence := _clinical_absence_message(result):
        answer = clinical_absence
    elif reason_code == "NO_RECORD_FOUND":
        answer = (
            "지정한 공식 코드와 조회 범위에서 확인 가능한 기록을 찾지 못했습니다. "
            "코드 또는 기간을 확인해 주세요."
        )
    elif reason_code == "INVALID_TOOL_ARGUMENTS":
        answer = (
            "공식 도구 호출 인자를 안전하게 확정하지 못해 조회를 중단했습니다. "
            "코드, 기간 또는 대상을 명시해 다시 요청해 주세요."
        )
    elif reason_code == "EVIDENCE_BINDING_FAILED":
        answer = (
            "공식 도구 결과와 답변 근거의 연결을 검증하지 못해 수치나 관계를 제공하지 않습니다. "
            "근거 연결이 확인되기 전에는 해당 주장을 제공할 수 없습니다."
        )
    elif reason_code == "TRUNCATED_RESULT":
        answer = (
            "공식 도구 결과가 절단되어 완전한 근거로 확인할 수 없습니다. "
            "조회 범위를 줄여 요청해 주세요."
        )
    else:
        answer = (
            "공식 도구 조회 결과를 확인할 수 없습니다. 도구 응답이 없거나 조회에 실패했습니다. "
            "코드, 기간 또는 대상을 확인해 주세요."
        )
    return AgentResult(
        status="typed_stop",
        answer=answer,
        tool_calls=result.tool_calls,
        sources=result.sources,
        traces=result.traces,
        fallback_code=None,
    )


def _clinical_absence_message(result: AgentResult) -> str | None:
    for call in result.tool_calls:
        if call.get("tool") != "clinicaltrials_v2_search":
            continue
        render_data = call.get("render_data")
        if not isinstance(render_data, dict):
            continue
        message = str(render_data.get("error_message") or "")
        if message.startswith("상태: 확인 불가"):
            return message
    return None


def normalize_execution_result(
    plan: RoutePlan,
    result: AgentResult,
) -> tuple[AgentResult, str | None]:
    proposed_count = len(plan.proposal.proposed_calls)
    call_statuses = tuple(str(call.get("status") or "unknown") for call in result.tool_calls)
    successful_count = sum(status == "ok" for status in call_statuses)

    if failed_call_scopes(plan, result) and any(
        isinstance((render_data := call.get("render_data")), dict)
        and str(render_data.get("error_code") or "").upper() == "PARTIAL_RESULT"
        for call in result.tool_calls
    ):
        return AgentResult(
            status="partial",
            answer=_partial_answer(plan, result),
            tool_calls=result.tool_calls,
            sources=result.sources,
            traces=result.traces,
            fallback_code=None,
        ), "PARTIAL_RESULT"

    if (
        plan.unresolvable_facets
        and proposed_count
        and len(call_statuses) == proposed_count
        and successful_count == proposed_count
    ):
        return AgentResult(
            status="partial",
            answer=_partial_evidence_answer(plan, result),
            tool_calls=result.tool_calls,
            sources=result.sources,
            traces=result.traces,
            fallback_code=None,
        ), "PARTIAL_EVIDENCE"

    if proposed_count and len(call_statuses) == proposed_count and successful_count == proposed_count:
        if result.status == "ok":
            return result, None
        return AgentResult(
            status="ok",
            answer=result.answer,
            tool_calls=result.tool_calls,
            sources=result.sources,
            traces=result.traces,
            fallback_code=None,
        ), None

    if successful_count:
        return AgentResult(
            status="partial",
            answer=_partial_answer(plan, result),
            tool_calls=result.tool_calls,
            sources=result.sources,
            traces=result.traces,
            fallback_code=None,
        ), "PARTIAL_RESULT"

    reason_code = execution_failure_reason(result)
    return safe_execution_failure(result, reason_code=reason_code), reason_code


def execution_failure_reason(result: AgentResult) -> str:
    error_codes = {
        str(render_data.get("error_code") or "").upper()
        for call in result.tool_calls
        if isinstance((render_data := call.get("render_data")), dict)
    }
    error_codes.discard("")
    if "TRUNCATED_RESULT" in error_codes:
        return "TRUNCATED_RESULT"
    if "IDENTITY_MISMATCH" in error_codes:
        return "IDENTITY_MISMATCH"
    if error_codes and error_codes <= {"NO_EVIDENCE", "NO_DATA"}:
        return "NO_RECORD_FOUND"
    if "SCHEMA_INVALID" in error_codes or (
        result.fallback_code is not None and result.fallback_code.value == "SCHEMA_INVALID"
    ):
        return "INVALID_TOOL_ARGUMENTS"
    return "UPSTREAM_UNAVAILABLE"


def claim_evidence_bindings(result: AgentResult) -> tuple[str, list[dict[str, Any]]]:
    if result.status not in {"ok", "partial"}:
        return "not_applicable", []

    expected_claims: list[tuple[str, EvidenceFact]] = []
    for call in result.tool_calls:
        if call.get("status") != "ok":
            continue
        render_data = call.get("render_data")
        evidence = render_data.get("evidence") if isinstance(render_data, dict) else None
        if not isinstance(evidence, list):
            continue
        for fact in evidence:
            if not isinstance(fact, dict):
                continue
            try:
                parsed = EvidenceFact.model_validate(fact)
            except ValueError:
                continue
            expected_claims.append((str(call.get("tool") or "unknown"), parsed))

    rendered_claims = tuple(
        line for line in result.answer.splitlines() if line.lstrip().startswith("- ")
    )
    expected_rendered = tuple(render_evidence_claim(fact) for _tool, fact in expected_claims)
    if rendered_claims != expected_rendered:
        return "fail", []

    bindings = [
        {
            "claim_ordinal": ordinal,
            "tool_name": tool_name,
            "evidence_ids": [fact.fact_id],
        }
        for ordinal, (tool_name, fact) in enumerate(expected_claims, start=1)
    ]
    expected = tuple(fact.fact_id for _tool_name, fact in expected_claims)
    bound = tuple(
        evidence_id
        for binding in bindings
        for evidence_id in binding["evidence_ids"]
    )
    valid = len(rendered_claims) == len(bindings) and verify_claim_evidence(
        expected_evidence_ids=expected,
        bound_evidence_ids=bound,
    )
    return ("pass" if valid else "fail"), bindings


def _partial_answer(plan: RoutePlan, result: AgentResult) -> str:
    missing = failed_call_scopes(plan, result)
    scope_text = ", ".join(missing) if missing else "요청 범위 일부"
    disclosure = (
        "상태: 일부 결과만 확인했습니다.\n"
        f"확인하지 못한 범위: {scope_text}\n"
        "대안: 누락된 범위를 다시 조회하거나 기간을 지정해 요청해 주세요."
    )
    return "\n\n".join(part for part in (result.answer.strip(), disclosure) if part)


def _partial_evidence_answer(plan: RoutePlan, result: AgentResult) -> str:
    notices = tuple(
        (
            "제품명이 없어 허가 정보는 조회할 수 없습니다. "
            "정확한 제품명을 알려주시면 확인하겠습니다."
        )
        if item.facet == "permission"
        else f"{item.facet} 범위는 조회 입력을 구성할 수 없습니다: {item.reason}"
        for item in plan.unresolvable_facets
    )
    disclosure = "\n".join(("상태: 일부 근거만 확인했습니다.", *notices))
    return "\n\n".join(part for part in (result.answer.strip(), disclosure) if part)


def failed_call_scopes(plan: RoutePlan, result: AgentResult) -> tuple[str, ...]:
    scopes: list[str] = []
    calls = tuple(result.tool_calls)
    for call in calls:
        render_data = call.get("render_data")
        if not isinstance(render_data, dict):
            continue
        if str(render_data.get("error_code") or "").upper() != "PARTIAL_RESULT":
            continue
        missing = render_data.get("missing_requested_facets")
        if isinstance(missing, list):
            scopes.extend(str(item) for item in missing if str(item).strip())
    for index, proposed in enumerate(plan.proposal.proposed_calls):
        status = str(calls[index].get("status") or "unknown") if index < len(calls) else "missing"
        if status == "ok":
            continue
        args = proposed.normalized_args
        public_value = next(
            (str(args[key]) for key in ("year", "period", "sick_cd", "brand", "query") if args.get(key)),
            f"요청 범위 {index + 1}",
        )
        scopes.append(public_value)
    return tuple(dict.fromkeys(scopes))
