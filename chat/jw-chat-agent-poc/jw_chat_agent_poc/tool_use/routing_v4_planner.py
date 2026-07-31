from __future__ import annotations

from dataclasses import replace
from typing import Any

from pydantic import ValidationError

from jw_chat_agent_poc.tool_use.provider import ToolChoiceProvider
from jw_chat_agent_poc.tool_use.routing_v4_capabilities import CapabilityMatrix, selection_source_for_eligible_tools
from jw_chat_agent_poc.tool_use.routing_v4_plan_support import (
    NoToolPlanRequest,
    RoutePlan,
    assert_eligible_tools_exist,
    call_route_plan,
    resolve_no_tool_route_plan,
    singleton_arguments,
    typed_message,
    typed_route_plan,
    validate_proposed_calls,
    validated_llm_tool_call,
    validated_tool_call,
)
from jw_chat_agent_poc.tool_use.routing_v4_rules import QuestionClassification, classify_question
from jw_chat_agent_poc.tool_use.routing_v4_types import (
    CapabilityStatus,
    ProposedRoutingSignature,
    RouteOutcome,
    RoutingDecision,
    RoutingMode,
    RoutingV4ContractError,
    ToolSelectionSource,
)
from jw_chat_agent_poc.tool_use.specs import ToolSpec


class ExternalRoutePlanner:
    def __init__(
        self,
        *,
        tools: tuple[ToolSpec, ...],
        provider: ToolChoiceProvider | None,
        capability_matrix: CapabilityMatrix,
    ) -> None:
        self._tools = tools
        self._by_name = {tool.name: tool for tool in tools}
        self._provider = provider
        self._capability_matrix = capability_matrix

    def plan(self, question: str, *, routing_mode: RoutingMode) -> RoutePlan:
        if routing_mode is RoutingMode.OFF:
            raise RoutingV4ContractError("the v4 planner must not run in OFF mode")
        classification = classify_question(question)
        classification = _with_legacy_input_key(classification)
        capability = self._capability_matrix.resolve(
            classification.source_domain,
            classification.requested_capability,
            input_key=classification.input_key,
        )
        if classification.unresolved_arguments:
            status = (
                CapabilityStatus.UNRESOLVED
                if capability.status is CapabilityStatus.SUPPORTED
                else capability.status
            )
            return typed_route_plan(
                routing_mode=routing_mode,
                classification=classification,
                status=status,
                reason_code="AMBIGUOUS_INPUT",
                eligible_tools=capability.eligible_tools,
            )
        if capability.status is CapabilityStatus.UNRESOLVED:
            return resolve_no_tool_route_plan(
                NoToolPlanRequest(
                    question=question,
                    routing_mode=routing_mode,
                    classification=classification,
                    capability_status=capability.status,
                ),
                self._provider,
            )
        if capability.status is not CapabilityStatus.SUPPORTED:
            return typed_route_plan(
                routing_mode=routing_mode,
                classification=classification,
                status=capability.status,
                reason_code=capability.typed_reason_code or "AMBIGUOUS_INPUT",
                eligible_tools=capability.eligible_tools,
            )
        eligible = classification.eligible_override or capability.eligible_tools
        assert_eligible_tools_exist(eligible, self._by_name)
        if classification.direct_calls:
            validate_proposed_calls(classification.direct_calls, self._by_name)
            return call_route_plan(
                routing_mode=routing_mode,
                classification=classification,
                capability_status=capability.status,
                selection_source=ToolSelectionSource.NEW_RULE,
                calls=classification.direct_calls,
                eligible_tools=eligible,
            )

        selection_source = selection_source_for_eligible_tools(len(eligible))
        if selection_source is ToolSelectionSource.NONE:
            raise RoutingV4ContractError("SUPPORTED capability resolved to zero eligible tools")
        if selection_source is ToolSelectionSource.DETERMINISTIC_SINGLETON:
            return self._singleton_plan(
                question=question,
                routing_mode=routing_mode,
                classification=classification,
                capability_status=capability.status,
                eligible_tools=eligible,
            )
        return self._llm_plan(
            question=question,
            routing_mode=routing_mode,
            classification=classification,
            capability_status=capability.status,
            eligible_tools=eligible,
        )

    def _singleton_plan(
        self,
        *,
        question: str,
        routing_mode: RoutingMode,
        classification: QuestionClassification,
        capability_status: CapabilityStatus,
        eligible_tools: tuple[str, ...],
    ) -> RoutePlan:
        tool_name = eligible_tools[0]
        arguments = singleton_arguments(tool_name, question)
        if arguments is None:
            return typed_route_plan(
                routing_mode=routing_mode,
                classification=classification,
                status=CapabilityStatus.UNRESOLVED,
                reason_code="AMBIGUOUS_INPUT",
                eligible_tools=eligible_tools,
            )
        call = validated_tool_call(tool_name, arguments, self._by_name)
        return call_route_plan(
            routing_mode=routing_mode,
            classification=classification,
            capability_status=capability_status,
            selection_source=ToolSelectionSource.DETERMINISTIC_SINGLETON,
            calls=(call.proposal,),
            eligible_tools=eligible_tools,
            execution_args=(call.execution_args,),
        )

    def _llm_plan(
        self,
        *,
        question: str,
        routing_mode: RoutingMode,
        classification: QuestionClassification,
        capability_status: CapabilityStatus,
        eligible_tools: tuple[str, ...],
    ) -> RoutePlan:
        if self._provider is None:
            return typed_route_plan(
                routing_mode=routing_mode,
                classification=classification,
                status=capability_status,
                reason_code="INVALID_TOOL_ARGUMENTS",
                eligible_tools=eligible_tools,
                selection_source=ToolSelectionSource.LLM,
            )
        candidate_names = set(eligible_tools)
        candidate_specs = tuple(tool for tool in self._tools if tool.name in candidate_names)
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "Choose at most one eligible tool. Do not invent missing codes, periods, or entities. "
                    "Return no tool when required arguments are not grounded in the user request."
                ),
            },
            {"role": "user", "content": question},
        ]
        repair_count = 0
        for attempt in range(2):
            choice = self._provider.choose(
                user_text=question,
                messages=messages,
                tools=[spec.openai_schema() for spec in candidate_specs],
            )
            if choice.name is None:
                decision = RoutingDecision(
                    source_domain=classification.source_domain,
                    domain_decision_source=classification.domain_decision_source,
                    capability_status=capability_status,
                    tool_selection_source=ToolSelectionSource.LLM,
                    route_outcome=RouteOutcome.NO_TOOL,
                )
                return RoutePlan(
                    proposal=ProposedRoutingSignature(
                        routing_mode=routing_mode,
                        routing_decision=decision,
                    ),
                    eligible_tools=eligible_tools,
                    input_key=classification.input_key,
                    reason_code="AMBIGUOUS_INPUT",
                    typed_message=typed_message("AMBIGUOUS_INPUT"),
                    repair_count=repair_count,
                    deterministic_rule_id=classification.deterministic_rule_id,
                    requested_facets=classification.requested_facets,
                    unresolvable_facets=classification.unresolvable_facets,
                )
            try:
                call = validated_llm_tool_call(choice, candidate_specs)
            except (KeyError, ValidationError, ValueError) as exc:
                if attempt == 1:
                    break
                repair_count = 1
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            f"The proposed tool call failed validation: {type(exc).__name__}. "
                            "Repair once using only an eligible schema."
                        ),
                    }
                )
                continue
            return call_route_plan(
                routing_mode=routing_mode,
                classification=classification,
                capability_status=capability_status,
                selection_source=ToolSelectionSource.LLM,
                calls=(call.proposal,),
                eligible_tools=eligible_tools,
                execution_args=(call.execution_args,),
                repair_count=repair_count,
            )
        return typed_route_plan(
            routing_mode=routing_mode,
            classification=classification,
            status=capability_status,
            reason_code="INVALID_TOOL_ARGUMENTS",
            eligible_tools=eligible_tools,
            selection_source=ToolSelectionSource.LLM,
            repair_count=repair_count,
        )


def _with_legacy_input_key(classification: QuestionClassification) -> QuestionClassification:
    if classification.input_key != "unknown" or not classification.direct_calls:
        return classification
    argument_keys = {
        key
        for call in classification.direct_calls
        for key in call.normalized_args
    }
    if "sick_cd" in argument_keys:
        return replace(classification, input_key="sick_cd")
    return classification
