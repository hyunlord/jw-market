from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import hashlib

from jw_chat_agent_poc.orchestrator.typed_failure import (
    TypedFailureCode,
    normalize_typed_failure,
)
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tool_use.integration import run_external_tool_agent
from jw_chat_agent_poc.tool_use.provider import ToolChoice
from jw_chat_agent_poc.tool_use.registry import ExternalToolRegistry
from jw_chat_agent_poc.tool_use.routing_v4 import (
    ExternalRoutePlanner,
    RoutingMode,
)
from jw_chat_agent_poc.tool_use.routing_v4_capabilities import default_capability_matrix
from jw_chat_agent_poc.tool_use.routing_v4_rules import classify_question
from jw_chat_agent_poc.tools.external import ExternalApiClient, ExternalCall


FB02 = "뇌경색 관련 임상시험이랑 허가 현황 알려줘"
PURE_CLINICAL = "뇌경색 관련 임상시험 알려줘"
PURE_PERMISSION = "아일리아 허가정보 알려줘"
PURE_CLINICAL_ANSWER_SHA256 = "30ffaf46e5f478ab5653d007a8b6971b8a8b652c3b79a5b67909a4fb9eeb8c03"
PURE_PERMISSION_ANSWER_SHA256 = "bd97f2262e12cfefc80de18cfb29b0cf5021dcbe5bb4e8f2c2cc1257285ffd29"


@dataclass(slots=True)
class _ChoiceSequence:
    choices: Sequence[ToolChoice]
    calls: int = field(default=0, init=False)

    def choose(self, *, user_text: str, messages: list[dict], tools: list[dict]) -> ToolChoice:
        del user_text, messages, tools
        choice = self.choices[self.calls]
        self.calls += 1
        return choice


def _clinical_choice() -> ToolChoice:
    return ToolChoice(
        "clinicaltrials_v2_search",
        {"query": "cerebral infarction", "query_type": "condition"},
        "global clinical source",
        call_id="fb02-clinical",
    )


def _external() -> ExternalApiClient:
    external = ExternalApiClient(mode="fixture")

    def clinical_search(
        query_intr: str,
        *,
        query_type: str = "intervention",
    ) -> ExternalCall:
        return ExternalCall(
            tool="clinicaltrials_v2_search",
            source="external_api",
            status="ok",
            summary_text="one study",
            render_data={
                "items": [{"NCTId": "NCT00000001", "briefTitle": "Stroke study"}],
                "request": {
                    "query.condition": query_intr,
                    "query_type": query_type,
                },
            },
        )

    def permission_search(brand: str) -> ExternalCall:
        return ExternalCall(
            tool="mfds_permission_search",
            source="external_api",
            status="ok",
            summary_text="one product",
            render_data={
                "items": [
                    {
                        "ITEM_SEQ": "item-1",
                        "ITEM_NAME": "아일리아주사",
                        "ENTP_NAME": "제조사",
                    }
                ],
                "request": {"brand": brand},
            },
        )

    external.clinicaltrials_v2_search = clinical_search
    external.mfds_permission_search = permission_search
    return external


def _planner(provider: _ChoiceSequence) -> ExternalRoutePlanner:
    external = _external()
    registry = ExternalToolRegistry(resolver=BrandResolver(), external=external)
    return ExternalRoutePlanner(
        tools=registry.list_for_query(FB02),
        provider=provider,
        capability_matrix=default_capability_matrix(),
    )


def _run(question: str, provider: _ChoiceSequence) -> dict:
    return run_external_tool_agent(
        question,
        resolver=BrandResolver(),
        external=_external(),
        provider=provider,
        routing_provider=provider,
    )


def test_fb02_classification_preserves_requested_and_unresolvable_facets() -> None:
    classification = classify_question(FB02)

    assert classification.requested_facets == ("clinical", "permission")
    assert tuple(item.facet for item in classification.unresolvable_facets) == ("permission",)
    assert classification.unresolvable_facets[0].reason == (
        "permission requires product_name, none found in question"
    )
    assert classification.requested_capability == "CLINICAL_TRIAL_SEARCH"


def test_fb02_plan_runs_clinical_without_constructing_a_disease_brand() -> None:
    plan = _planner(_ChoiceSequence((_clinical_choice(),))).plan(
        FB02,
        routing_mode=RoutingMode.ENFORCE,
    )

    assert plan.requested_facets == ("clinical", "permission")
    assert tuple(item.facet for item in plan.unresolvable_facets) == ("permission",)
    assert [call.tool_name for call in plan.proposal.proposed_calls] == [
        "clinicaltrials_v2_search"
    ]
    assert all("brand" not in call.normalized_args for call in plan.proposal.proposed_calls)


def test_fb02_successful_clinical_result_is_partial_evidence(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    payload = _run(FB02, _ChoiceSequence((_clinical_choice(),)))

    assert payload["agent_loop_metrics"]["status"] == "partial"
    assert [call["tool"] for call in payload["tool_calls"]] == [
        "clinicaltrials_v2_search"
    ]
    assert "NCT00000001" in payload["answer"]
    assert "제품명이 없어 허가 정보는 조회할 수 없습니다" in payload["answer"]
    assert "정확한 제품명을 알려주시면 확인하겠습니다" in payload["answer"]
    routing = payload["router_diagnostics"]["routing_v4"]
    assert routing["requested_facets"] == ["clinical", "permission"]
    assert routing["unresolvable_facets"] == [
        {
            "facet": "permission",
            "reason": "permission requires product_name, none found in question",
        }
    ]
    assert routing["executed_call_signature"]["reason_code"] == "PARTIAL_EVIDENCE"
    typed = normalize_typed_failure(payload)
    assert typed is not None
    assert typed.code is TypedFailureCode.PARTIAL_EVIDENCE
    assert typed.partial is True
    assert typed.terminal is False


def test_pure_clinical_and_permission_answers_remain_byte_identical(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")

    clinical = _run(PURE_CLINICAL, _ChoiceSequence((_clinical_choice(),)))
    permission = _run(PURE_PERMISSION, _ChoiceSequence(()))

    assert hashlib.sha256(clinical["answer"].encode()).hexdigest() == (
        PURE_CLINICAL_ANSWER_SHA256
    )
    assert hashlib.sha256(permission["answer"].encode()).hexdigest() == (
        PURE_PERMISSION_ANSWER_SHA256
    )
    assert classify_question(PURE_CLINICAL).requested_facets == ("clinical",)
    assert classify_question(PURE_CLINICAL).unresolvable_facets == ()
    assert classify_question(PURE_PERMISSION).requested_facets == ("permission",)
    assert classify_question(PURE_PERMISSION).unresolvable_facets == ()
