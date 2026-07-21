from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from jw_chat_agent_poc.tool_use.provider import ToolChoice, ToolChoiceProvider
from jw_chat_agent_poc.tool_use.routing_v4_rules import PREFIX_RE, QuestionClassification, explicit_disease_code
from jw_chat_agent_poc.tool_use.routing_v4_types import (
    CapabilityStatus,
    ProposedCall,
    ProposedRoutingSignature,
    RouteOutcome,
    RoutingDecision,
    RoutingMode,
    RoutingV4ContractError,
    ToolSelectionSource,
    proposed_call_key,
)
from jw_chat_agent_poc.tool_use.specs import ToolSpec


GENERAL_HELP_MESSAGE = (
    "시장, 브랜드, 기간, 지표를 포함해 질문하면 확인 가능한 근거를 조회해 답합니다. "
    "필수 정보가 모호하면 부족한 항목만 다시 확인합니다."
)
_PERIOD_ARGUMENT_KEYS = frozenset({"period", "year"})


class RoutePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal: ProposedRoutingSignature
    eligible_tools: tuple[str, ...]
    reason_code: str | None
    typed_message: str | None
    repair_count: int = 0
    deterministic_rule_id: str | None = None


@dataclass(frozen=True, slots=True)
class NoToolPlanRequest:
    question: str
    routing_mode: RoutingMode
    classification: QuestionClassification
    capability_status: CapabilityStatus


def assert_eligible_tools_exist(
    eligible_tools: tuple[str, ...],
    by_name: dict[str, ToolSpec],
) -> None:
    missing = tuple(name for name in eligible_tools if name not in by_name)
    if missing:
        raise RoutingV4ContractError(f"capability matrix references missing tools: {missing}")


def validate_proposed_calls(
    calls: tuple[ProposedCall, ...],
    by_name: dict[str, ToolSpec],
) -> None:
    seen: set[str] = set()
    validated_calls: list[ProposedCall] = []
    for call in calls:
        validated = validated_call(call.tool_name, call.normalized_args, by_name)
        key = proposed_call_key(validated)
        if key in seen:
            raise RoutingV4ContractError("duplicate canonical tool and arguments")
        seen.add(key)
        validated_calls.append(validated)
    _validate_period_call_exception(tuple(validated_calls))


def _validate_period_call_exception(calls: tuple[ProposedCall, ...]) -> None:
    if len(calls) <= 1:
        return
    if len(calls) > 5:
        raise RoutingV4ContractError("period call exception exceeds five authority calls")
    if len({call.tool_name for call in calls}) != 1:
        raise RoutingV4ContractError("period call exception requires the same authority tool")

    stable_arguments = {
        json.dumps(
            {
                key: value
                for key, value in call.normalized_args.items()
                if key not in _PERIOD_ARGUMENT_KEYS
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for call in calls
    }
    if len(stable_arguments) != 1:
        raise RoutingV4ContractError(
            "period call exception requires identical non-period arguments"
        )

    period_arguments = tuple(
        {
            key: value
            for key, value in call.normalized_args.items()
            if key in _PERIOD_ARGUMENT_KEYS
        }
        for call in calls
    )
    if any(not arguments for arguments in period_arguments):
        raise RoutingV4ContractError("period call exception requires period arguments")
    canonical_periods = {
        json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for arguments in period_arguments
    }
    if len(canonical_periods) != len(calls):
        raise RoutingV4ContractError("period call exception requires unique period arguments")


def validated_call(
    tool_name: str,
    arguments: dict[str, Any],
    by_name: dict[str, ToolSpec],
) -> ProposedCall:
    spec = by_name[tool_name]
    payload = spec.input_model.model_validate(arguments)
    return ProposedCall(
        tool_name=tool_name,
        normalized_args=normalize_arguments(payload.model_dump(mode="json")),
    )


def validated_llm_choice(
    choice: ToolChoice,
    candidate_specs: tuple[ToolSpec, ...],
) -> ProposedCall:
    by_name = {spec.name: spec for spec in candidate_specs}
    if choice.name not in by_name:
        raise RoutingV4ContractError("planner selected a tool outside the eligible capability set")
    return validated_call(choice.name, choice.arguments, by_name)


def normalize_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str):
            compact = re.sub(r"\s+", " ", value.strip())
            normalized[key] = explicit_disease_code(compact) if key == "sick_cd" else compact
            if normalized[key] is None:
                normalized[key] = compact
        else:
            normalized[key] = value
    return normalized


def singleton_arguments(tool_name: str, question: str) -> dict[str, Any] | None:
    if tool_name in {"mfds_permission_search", "mfds_composition"}:
        body = PREFIX_RE.sub("", question, count=1).strip()
        match = re.match(r"(?P<subject>[A-Za-z가-힣0-9+_-]{2,80}?)(?:의|\s)", body)
        return {"brand": match.group("subject").strip()} if match else None
    return None


def call_route_plan(
    *,
    routing_mode: RoutingMode,
    classification: QuestionClassification,
    capability_status: CapabilityStatus,
    selection_source: ToolSelectionSource,
    calls: tuple[ProposedCall, ...],
    eligible_tools: tuple[str, ...],
    repair_count: int = 0,
) -> RoutePlan:
    decision = RoutingDecision(
        source_domain=classification.source_domain,
        domain_decision_source=classification.domain_decision_source,
        capability_status=capability_status,
        tool_selection_source=selection_source,
        route_outcome=RouteOutcome.CALL,
    )
    return RoutePlan(
        proposal=ProposedRoutingSignature(
            routing_mode=routing_mode,
            routing_decision=decision,
            proposed_calls=calls,
        ),
        eligible_tools=eligible_tools,
        reason_code=None,
        typed_message=None,
        repair_count=repair_count,
        deterministic_rule_id=classification.deterministic_rule_id,
    )


def no_tool_route_plan(
    *,
    routing_mode: RoutingMode,
    classification: QuestionClassification,
    status: CapabilityStatus,
    message: str,
) -> RoutePlan:
    decision = RoutingDecision(
        source_domain=classification.source_domain,
        domain_decision_source=classification.domain_decision_source,
        capability_status=status,
        tool_selection_source=ToolSelectionSource.NONE,
        route_outcome=RouteOutcome.NO_TOOL,
    )
    return RoutePlan(
        proposal=ProposedRoutingSignature(
            routing_mode=routing_mode,
            routing_decision=decision,
        ),
        eligible_tools=(),
        reason_code=None,
        typed_message=message,
        deterministic_rule_id=classification.deterministic_rule_id,
    )


def resolve_no_tool_route_plan(
    request: NoToolPlanRequest,
    provider: ToolChoiceProvider | None,
) -> RoutePlan:
    del provider
    return no_tool_route_plan(
        routing_mode=request.routing_mode,
        classification=request.classification,
        status=request.capability_status,
        message=GENERAL_HELP_MESSAGE,
    )


def typed_route_plan(
    *,
    routing_mode: RoutingMode,
    classification: QuestionClassification,
    status: CapabilityStatus,
    reason_code: str,
    eligible_tools: tuple[str, ...],
    selection_source: ToolSelectionSource = ToolSelectionSource.NONE,
    repair_count: int = 0,
) -> RoutePlan:
    decision = RoutingDecision(
        source_domain=classification.source_domain,
        domain_decision_source=classification.domain_decision_source,
        capability_status=status,
        tool_selection_source=selection_source,
        route_outcome=RouteOutcome.TYPED_STOP,
    )
    return RoutePlan(
        proposal=ProposedRoutingSignature(
            routing_mode=routing_mode,
            routing_decision=decision,
        ),
        eligible_tools=eligible_tools,
        reason_code=reason_code,
        typed_message=typed_message(reason_code),
        repair_count=repair_count,
        deterministic_rule_id=classification.deterministic_rule_id,
    )


def typed_message(reason_code: str) -> str:
    messages = {
        "CAPABILITY_NOT_IMPLEMENTED": (
            "요청한 기능은 현재 연결된 공식 도구에 구현되어 있지 않아 확인할 수 없습니다. "
            "관련 공식 기준 문서나 조회 범위를 지정해 주세요."
        ),
        "FIELD_NOT_EXPOSED": (
            "지정한 공식 도구는 요청한 상세 필드를 제공하지 않아 확인할 수 없습니다. "
            "현재 조회 가능한 기본 항목으로 범위를 바꿔 주세요."
        ),
        "AMBIGUOUS_INPUT": (
            "요청 대상을 하나로 확정할 수 없어 조회하지 않았습니다. "
            "공식 코드나 제품명을 한 가지로 지정해 주세요."
        ),
        "INVALID_TOOL_ARGUMENTS": (
            "공식 도구 호출 인자를 안전하게 확정하지 못해 조회를 중단했습니다. "
            "코드, 기간 또는 대상을 명시해 다시 요청해 주세요."
        ),
    }
    return messages.get(reason_code, "요청을 안전하게 처리할 근거를 확정하지 못했습니다.")
