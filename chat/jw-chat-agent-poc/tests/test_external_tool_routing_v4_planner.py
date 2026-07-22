from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tool_use.provider import ToolChoice
from jw_chat_agent_poc.tool_use.registry import ExternalToolRegistry
from jw_chat_agent_poc.tool_use.routing_v4_plan_support import validate_proposed_calls
from jw_chat_agent_poc.tool_use.routing_v4 import (
    CapabilityMatrix,
    CapabilityStatus,
    DomainDecisionSource,
    ExternalRoutePlanner,
    ProposedCall,
    RouteOutcome,
    RoutingDecision,
    RoutingMode,
    ToolSelectionSource,
)
from jw_chat_agent_poc.tools.external import ExternalApiClient
from jw_chat_agent_poc.tool_use.routing_v4_types import RoutingV4ContractError
from jw_chat_agent_poc.tool_use.specs import ToolSpec


CONTRACT_DIR = Path(__file__).parent / "contracts" / "external_tool_routing_v4"


@dataclass(slots=True)
class _ChoiceSequence:
    choices: Sequence[ToolChoice]
    calls: int = field(default=0, init=False)
    visible_tool_names: list[tuple[str, ...]] = field(default_factory=list, init=False)

    def choose(self, *, user_text: str, messages: list[dict], tools: list[dict]) -> ToolChoice:
        del user_text, messages
        self.visible_tool_names.append(tuple(str(tool["function"]["name"]) for tool in tools))
        choice = self.choices[self.calls]
        self.calls += 1
        return choice


def _planner(provider: _ChoiceSequence | None = None) -> ExternalRoutePlanner:
    registry = ExternalToolRegistry(
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
    )
    return ExternalRoutePlanner(
        tools=registry.list_for_query("external routing v4 contract"),
        provider=provider,
        capability_matrix=CapabilityMatrix.from_json(CONTRACT_DIR / "capability_matrix.json"),
    )


def _tool_specs_by_name() -> dict[str, ToolSpec]:
    registry = ExternalToolRegistry(
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
    )
    return {tool.name: tool for tool in registry.list_for_query("external routing v4 contract")}


def test_a01_direct_disease_code_uses_new_rule_and_five_unique_period_calls() -> None:
    plan = _planner().plan(
        "상병코드 D693의 최근 5개년 환자수 추이를 분석해줘",
        routing_mode=RoutingMode.SHADOW,
    )

    assert plan.proposal.routing_decision == RoutingDecision(
        source_domain="hira",
        domain_decision_source=DomainDecisionSource.INTENT_OWNER,
        capability_status=CapabilityStatus.SUPPORTED,
        tool_selection_source=ToolSelectionSource.NEW_RULE,
        route_outcome=RouteOutcome.CALL,
    )
    assert len(plan.proposal.proposed_calls) == 5
    assert {call.tool_name for call in plan.proposal.proposed_calls} == {
        "hira_disease_hospitalization_outpatient_stats"
    }
    assert {call.normalized_args["sick_cd"] for call in plan.proposal.proposed_calls} == {"D69.3"}
    assert [call.normalized_args["year"] for call in plan.proposal.proposed_calls] == [
        "2020",
        "2021",
        "2022",
        "2023",
        "2024",
    ]
    assert plan.input_key == "sick_cd"


def test_authoritative_disease_name_mapping_routes_without_brand_substitution() -> None:
    plan = _planner().plan(
        "고지혈증 환자수 추이 알려줘",
        routing_mode=RoutingMode.SHADOW,
    )

    assert plan.input_key == "disease_name"
    assert plan.proposal.routing_decision.capability_status is CapabilityStatus.SUPPORTED
    assert plan.proposal.routing_decision.tool_selection_source is ToolSelectionSource.NEW_RULE
    assert len(plan.proposal.proposed_calls) == 5
    assert {call.normalized_args["sick_cd"] for call in plan.proposal.proposed_calls} == {"E78"}
    assert "brand" not in str(plan.proposal.proposed_calls)


def test_period_call_exception_rejects_multiple_authority_tool_types() -> None:
    calls = (
        ProposedCall(
            tool_name="hira_disease_hospitalization_outpatient_stats",
            normalized_args={"sick_cd": "D69.3", "year": "2023"},
        ),
        ProposedCall(
            tool_name="mfds_permission_search",
            normalized_args={"brand": "아일리아"},
        ),
    )

    with pytest.raises(RoutingV4ContractError, match="same authority tool"):
        validate_proposed_calls(calls, _tool_specs_by_name())


def test_period_call_exception_rejects_target_drift_between_periods() -> None:
    calls = (
        ProposedCall(
            tool_name="hira_disease_hospitalization_outpatient_stats",
            normalized_args={"sick_cd": "D69.3", "year": "2023"},
        ),
        ProposedCall(
            tool_name="hira_disease_hospitalization_outpatient_stats",
            normalized_args={"sick_cd": "E11", "year": "2024"},
        ),
    )

    with pytest.raises(RoutingV4ContractError, match="non-period arguments"):
        validate_proposed_calls(calls, _tool_specs_by_name())


def test_a03_explicit_compact_code_never_falls_back_to_parent_code() -> None:
    plan = _planner().plan(
        "질병코드 H360 환자수 통계 알려줘",
        routing_mode=RoutingMode.ENFORCE,
    )

    assert plan.proposal.proposed_calls == (
        ProposedCall(
            tool_name="hira_disease_hospitalization_outpatient_stats",
            normalized_args={"sick_cd": "H36.0", "year": "2024"},
        ),
    )
    assert "H36" not in {
        str(call.normalized_args["sick_cd"])
        for call in plan.proposal.proposed_calls
    }
    assert plan.input_key == "sick_cd"


def test_parent_disease_code_is_not_promoted_to_a_child_code() -> None:
    plan = _planner().plan(
        "질병코드 H36 환자수 통계 알려줘",
        routing_mode=RoutingMode.SHADOW,
    )

    assert plan.proposal.proposed_calls[0].normalized_args["sick_cd"] == "H36"
    assert plan.proposal.proposed_calls[0].normalized_args["sick_cd"] != "H36.0"


@pytest.mark.parametrize(
    "question",
    (
        "아일리아의 급여기준에 대해서 적응증 별로 설명해줘",
        "Eylea 급여기준 알려줘",
        "Aflibercept 급여기준 알려줘",
    ),
)
def test_reimbursement_requests_stop_as_not_implemented_without_web(question: str) -> None:
    plan = _planner().plan(question, routing_mode=RoutingMode.ENFORCE)

    assert plan.proposal.routing_decision.capability_status is CapabilityStatus.NOT_IMPLEMENTED
    assert plan.proposal.routing_decision.tool_selection_source is ToolSelectionSource.NONE
    assert plan.proposal.routing_decision.route_outcome is RouteOutcome.TYPED_STOP
    assert plan.proposal.proposed_calls == ()
    assert plan.reason_code == "CAPABILITY_NOT_IMPLEMENTED"
    assert "web_search" not in plan.eligible_tools


def test_hira_prefix_does_not_switch_source_to_answer_an_unsupported_field() -> None:
    plan = _planner().plan("HIRA: 아일리아 효능 알려줘", routing_mode=RoutingMode.ENFORCE)

    assert plan.proposal.routing_decision == RoutingDecision(
        source_domain="hira",
        domain_decision_source=DomainDecisionSource.PREFIX_RULE,
        capability_status=CapabilityStatus.FIELD_NOT_EXPOSED,
        tool_selection_source=ToolSelectionSource.NONE,
        route_outcome=RouteOutcome.TYPED_STOP,
    )
    assert plan.reason_code == "FIELD_NOT_EXPOSED"
    assert plan.proposal.proposed_calls == ()


def test_a07_many_product_family_stops_as_ambiguous_before_field_gap() -> None:
    plan = _planner().plan(
        "NeDrug: 아일리아 제품의 효능·효과, 용법·용량, 사용상 주의사항을 알려줘",
        routing_mode=RoutingMode.ENFORCE,
    )

    assert plan.proposal.routing_decision == RoutingDecision(
        source_domain="regulatory",
        domain_decision_source=DomainDecisionSource.PREFIX_RULE,
        capability_status=CapabilityStatus.FIELD_NOT_EXPOSED,
        tool_selection_source=ToolSelectionSource.NONE,
        route_outcome=RouteOutcome.TYPED_STOP,
    )
    assert plan.reason_code == "AMBIGUOUS_INPUT"
    assert plan.proposal.proposed_calls == ()
    assert "제품명" in str(plan.typed_message)
    assert "web_search" not in plan.eligible_tools


@pytest.mark.parametrize(
    "question",
    (
        "NCT05151731의 inclusion 및 exclusion Criteria 알려줘",
        "NCT05151731 임상 디자인(대상, 평가변수, 기간)을 알려줘",
    ),
)
def test_nct_detail_requests_use_verified_detail_tool(question: str) -> None:
    plan = _planner().plan(question, routing_mode=RoutingMode.ENFORCE)

    assert plan.proposal.routing_decision.source_domain == "clinical_trials"
    assert plan.proposal.routing_decision.capability_status is CapabilityStatus.SUPPORTED
    assert plan.proposal.routing_decision.route_outcome is RouteOutcome.CALL
    assert plan.reason_code is None
    assert [call.tool_name for call in plan.proposal.proposed_calls] == ["clinicaltrials_study_details"]
    assert plan.input_key == "nct_id"


def test_verified_mfds_composition_uses_the_single_contract_backed_tool() -> None:
    plan = _planner().plan(
        "NeDrug: 리바로 성분 조성 알려줘",
        routing_mode=RoutingMode.ENFORCE,
    )

    assert plan.proposal.routing_decision == RoutingDecision(
        source_domain="regulatory",
        domain_decision_source=DomainDecisionSource.PREFIX_RULE,
        capability_status=CapabilityStatus.SUPPORTED,
        tool_selection_source=ToolSelectionSource.DETERMINISTIC_SINGLETON,
        route_outcome=RouteOutcome.CALL,
    )
    assert plan.proposal.proposed_calls == (
        ProposedCall(tool_name="mfds_composition", normalized_args={"brand": "리바로"}),
    )


def test_easy_drug_label_fields_remain_field_not_exposed() -> None:
    plan = _planner().plan(
        "NeDrug: 리바로 e약 효능 알려줘",
        routing_mode=RoutingMode.ENFORCE,
    )

    assert plan.proposal.routing_decision.capability_status is CapabilityStatus.FIELD_NOT_EXPOSED
    assert plan.proposal.routing_decision.route_outcome is RouteOutcome.TYPED_STOP
    assert plan.proposal.proposed_calls == ()
    assert plan.input_key == "product_name"


def test_known_ingredient_routes_openfda_adverse_event_without_brand_gate() -> None:
    plan = _planner().plan(
        "pitavastatin 부작용 알려줘",
        routing_mode=RoutingMode.SHADOW,
    )

    assert plan.input_key == "ingredient"
    assert plan.eligible_tools == ("openfda_label_search",)
    assert plan.proposal.routing_decision.tool_selection_source is ToolSelectionSource.NEW_RULE
    assert plan.proposal.proposed_calls == (
        ProposedCall(
            tool_name="openfda_label_search",
            normalized_args={"ingredient": "Pitavastatin", "evidence_type": "adverse_event"},
        ),
    )


def test_known_ingredient_patent_question_exposes_only_patent_tools_to_llm() -> None:
    provider = _ChoiceSequence(
        (
            ToolChoice(
                "mfds_patent",
                {"ingredient": "  Pitavastatin  "},
                "국내 특허 조회",
                call_id="proposal-1",
            ),
        )
    )

    plan = _planner(provider).plan(
        "pitavastatin 특허 만료일 알려줘",
        routing_mode=RoutingMode.SHADOW,
    )

    assert plan.input_key == "ingredient"
    assert provider.visible_tool_names == [("mfds_patent", "mfds_fda_orangebook")]
    assert plan.proposal.routing_decision.tool_selection_source is ToolSelectionSource.LLM
    assert plan.proposal.proposed_calls[0].normalized_args == {"ingredient": "Pitavastatin"}
    assert plan.execution_args == ({"ingredient": "  Pitavastatin  "},)


def test_a13_one_eligible_tool_is_selected_without_calling_llm_provider() -> None:
    provider = _ChoiceSequence(
        (ToolChoice("web_search", {"query": "must not run"}, "wrong", call_id="wrong"),)
    )

    plan = _planner(provider).plan(
        "아일리아의 허가 품목명과 업체명을 공식 허가정보 기준으로 알려줘",
        routing_mode=RoutingMode.ENFORCE,
    )

    assert provider.calls == 0
    assert plan.eligible_tools == ("mfds_permission_search",)
    assert plan.proposal.routing_decision.tool_selection_source is ToolSelectionSource.DETERMINISTIC_SINGLETON
    assert plan.proposal.proposed_calls == (
        ProposedCall(tool_name="mfds_permission_search", normalized_args={"brand": "아일리아"}),
    )


def test_a10_llm_sees_only_the_two_eligible_clinical_trial_tools() -> None:
    provider = _ChoiceSequence(
        (
            ToolChoice(
                "clinicaltrials_v2_search",
                {"query": "diabetic macular edema", "query_type": "condition"},
                "select global registry",
                call_id="proposal-1",
            ),
        )
    )

    plan = _planner(provider).plan(
        "당뇨병성 황반부종(DME) 관련 임상시험을 찾아줘",
        routing_mode=RoutingMode.SHADOW,
    )

    assert provider.calls == 1
    assert provider.visible_tool_names == [
        ("mfds_clinical_trial_kr", "clinicaltrials_v2_search")
    ]
    assert plan.proposal.routing_decision.domain_decision_source is DomainDecisionSource.LLM
    assert plan.proposal.routing_decision.tool_selection_source is ToolSelectionSource.LLM
    assert plan.proposal.proposed_calls == (
        ProposedCall(
            tool_name="clinicaltrials_v2_search",
            normalized_args={"query": "diabetic macular edema", "query_type": "condition"},
        ),
    )
    assert plan.input_key == "natural_query"


def test_llm_execution_arguments_are_not_replaced_by_comparison_normalization() -> None:
    provider = _ChoiceSequence(
        (
            ToolChoice(
                "clinicaltrials_v2_search",
                {"query": "  diabetic   macular edema  ", "query_type": "condition"},
                "bounded query",
                call_id="proposal-raw",
            ),
        )
    )

    plan = _planner(provider).plan(
        "당뇨병성 황반부종 관련 임상시험을 찾아줘",
        routing_mode=RoutingMode.ENFORCE,
    )

    assert plan.proposal.proposed_calls[0].normalized_args["query"] == "diabetic macular edema"
    assert plan.execution_args == (
        {"query": "  diabetic   macular edema  ", "query_type": "condition"},
    )


def test_invalid_llm_arguments_get_exactly_one_schema_repair() -> None:
    provider = _ChoiceSequence(
        (
            ToolChoice("clinicaltrials_v2_search", {"unexpected": "bad"}, "bad", call_id="proposal-1"),
            ToolChoice(
                "clinicaltrials_v2_search",
                {"query": "diabetic macular edema", "query_type": "condition"},
                "repaired",
                call_id="proposal-2",
            ),
        )
    )

    plan = _planner(provider).plan(
        "당뇨병성 황반부종(DME) 관련 임상시험을 찾아줘",
        routing_mode=RoutingMode.ENFORCE,
    )

    assert provider.calls == 2
    assert plan.repair_count == 1
    assert plan.reason_code is None
    assert plan.proposal.routing_decision.route_outcome is RouteOutcome.CALL


def test_second_invalid_llm_proposal_stops_without_filling_missing_arguments() -> None:
    provider = _ChoiceSequence(
        (
            ToolChoice("clinicaltrials_v2_search", {"unexpected": "bad"}, "bad", call_id="proposal-1"),
            ToolChoice("web_search", {"query": "escape"}, "also bad", call_id="proposal-2"),
        )
    )

    plan = _planner(provider).plan(
        "당뇨병성 황반부종(DME) 관련 임상시험을 찾아줘",
        routing_mode=RoutingMode.ENFORCE,
    )

    assert provider.calls == 2
    assert plan.repair_count == 1
    assert plan.reason_code == "INVALID_TOOL_ARGUMENTS"
    assert plan.proposal.routing_decision.route_outcome is RouteOutcome.TYPED_STOP
    assert plan.proposal.proposed_calls == ()


def test_d10_general_help_question_is_no_tool_not_missing_capability() -> None:
    provider = _ChoiceSequence((ToolChoice(None, {}, "검증되지 않은 외부 사실을 답합니다.", call_id=None),))

    plan = _planner(provider).plan(
        "이 챗봇 어떻게 쓰는 거야?",
        routing_mode=RoutingMode.ENFORCE,
    )

    assert provider.calls == 0
    assert provider.visible_tool_names == []
    assert plan.proposal.routing_decision.capability_status is CapabilityStatus.UNRESOLVED
    assert plan.proposal.routing_decision.tool_selection_source is ToolSelectionSource.NONE
    assert plan.proposal.routing_decision.route_outcome is RouteOutcome.NO_TOOL
    assert plan.proposal.proposed_calls == ()
    assert plan.reason_code is None
    assert plan.typed_message != "검증되지 않은 외부 사실을 답합니다."
    assert "시장" in plan.typed_message
    assert "브랜드" in plan.typed_message
