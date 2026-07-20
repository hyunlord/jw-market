from __future__ import annotations

from typing import Any

from jw_chat_agent_poc.tool_use.contracts import AgentResult, EvidenceFact
from jw_chat_agent_poc.tool_use.renderer import render_evidence_claim
from jw_chat_agent_poc.tool_use.routing_v4_capabilities import verify_claim_evidence
from jw_chat_agent_poc.tool_use.routing_v4_plan_support import RoutePlan


def safe_execution_failure(result: AgentResult, *, reason_code: str) -> AgentResult:
    if reason_code == "NO_RECORD_FOUND":
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


def normalize_execution_result(
    plan: RoutePlan,
    result: AgentResult,
) -> tuple[AgentResult, str | None]:
    proposed_count = len(plan.proposal.proposed_calls)
    call_statuses = tuple(str(call.get("status") or "unknown") for call in result.tool_calls)
    successful_count = sum(status == "ok" for status in call_statuses)

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
    missing = _failed_call_scopes(plan, result)
    scope_text = ", ".join(missing) if missing else "요청 범위 일부"
    disclosure = (
        "상태: 일부 결과만 확인했습니다.\n"
        f"확인하지 못한 범위: {scope_text}\n"
        "대안: 누락된 범위를 다시 조회하거나 기간을 지정해 요청해 주세요."
    )
    return "\n\n".join(part for part in (result.answer.strip(), disclosure) if part)


def _failed_call_scopes(plan: RoutePlan, result: AgentResult) -> tuple[str, ...]:
    scopes: list[str] = []
    calls = tuple(result.tool_calls)
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
