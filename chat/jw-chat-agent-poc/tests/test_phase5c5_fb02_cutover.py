from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from jw_chat_agent_poc.orchestrator.typed_failure import (
    TypedFailureCode,
    normalize_typed_failure,
)
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import SessionStore, compute_final_answer
from jw_chat_agent_poc.service.genos_client import GenosClient
from jw_chat_agent_poc.service import unified_router_cutover as cutover
from jw_chat_agent_poc.tool_use.integration import run_external_tool_agent
from jw_chat_agent_poc.tool_use.provider import ToolChoice
from jw_chat_agent_poc.tool_use.routing_v4_rules import classify_question
from jw_chat_agent_poc.tool_use.specs import BrandInput
from jw_chat_agent_poc.tools.external import ExternalApiClient, ExternalCall


FB02 = "뇌경색 관련 임상시험이랑 허가 현황 알려줘"


class _MarketScopeStub:
    def has_explicit_brand_anchor(self, question: str) -> bool:
        return "리바로" in question

    def has_explicit_named_market(self, question: str) -> bool:
        return "시장" in question


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
        call_id="phase5c5-fb02",
    )


def _external() -> ExternalApiClient:
    external = ExternalApiClient(mode="fixture")

    def clinical_search(query_intr: str, *, query_type: str = "intervention") -> ExternalCall:
        return ExternalCall(
            tool="clinicaltrials_v2_search",
            source="external_api",
            status="ok",
            summary_text="one study",
            render_data={
                "items": [{"NCTId": "NCT00000001", "briefTitle": "Stroke study"}],
                "request": {"query.condition": query_intr, "query_type": query_type},
            },
        )

    external.clinicaltrials_v2_search = clinical_search
    return external


class _Fb02Agent:
    def answer(self, question: str, documents, **_kwargs):
        assert documents is None
        provider = _ChoiceSequence((_clinical_choice(),))
        return run_external_tool_agent(
            question,
            resolver=BrandResolver(),
            external=_external(),
            provider=provider,
            routing_provider=provider,
        )


def test_fb02_cutover_preserves_all_eight_contract_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    monkeypatch.setenv(cutover.CLINICAL_TRIALS_CUTOVER_ENV, "1")
    monkeypatch.setenv(cutover.CLINICAL_FB02_CUTOVER_ENV, "1")

    def forbidden_brand(*_args, **_kwargs):
        raise AssertionError("FB02 disease text reached BrandInput.brand")

    monkeypatch.setattr(BrandInput, "model_validate", forbidden_brand)
    result = service_app._answer_existing_without_pending(
        _MarketScopeStub(),
        lambda **_kwargs: _Fb02Agent(),
        "conversation",
        FB02,
        "fixture",
        None,
        SessionStore(),
        use_direct_agent_loop=True,
    )

    routing = result["router_diagnostics"]["routing_v4"]
    partial = routing["partial_evidence"]
    typed = normalize_typed_failure(result)
    assert routing["requested_facets"] == ["clinical", "permission"]
    assert [item["facet"] for item in routing["unresolvable_facets"]] == ["permission"]
    assert result["agent_loop_metrics"]["status"] == "partial"
    assert typed is not None and typed.code is TypedFailureCode.PARTIAL_EVIDENCE
    assert partial["producer"] == "unresolvable_facet"
    assert "NCT00000001" in result["answer"]
    assert "제품명이 없어 허가 정보는 조회할 수 없습니다" in result["answer"]
    assert [call["tool"] for call in result["tool_calls"]] == ["clinicaltrials_v2_search"]
    assert "NCT00000001" in result["answer"] and "허가 정보" in result["answer"]
    assert result["router_diagnostics"]["canonical_router_cutover"]["handler"] == (
        "CLINICAL_TRIAL_SEARCH"
    )

    expected = result["answer"]

    def unexpected_stream(*_args, **_kwargs):
        raise AssertionError("PARTIAL_EVIDENCE must not be replaced by LLM synthesis")

    monkeypatch.setattr(GenosClient, "stream_answer", unexpected_stream)
    final = compute_final_answer(FB02, result, "phase5c5-fb02-cutover")
    assert final.text == expected
    assert final.trace["qa_trace"]["answer_delivery"]["answer_branch"] == "typed_partial"


def test_fb02_static_plan_contains_no_brand_argument() -> None:
    classification = classify_question(FB02)
    assert classification.requested_facets == ("clinical", "permission")
    assert tuple(item.facet for item in classification.unresolvable_facets) == ("permission",)
