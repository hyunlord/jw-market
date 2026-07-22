from __future__ import annotations

import pytest

from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tool_use import integration as integration_module
from jw_chat_agent_poc.tool_use.integration import (
    _deterministic_tool_choices,
    run_external_tool_agent,
)
from jw_chat_agent_poc.tools.external import ExternalApiClient


class _UnexpectedPlanner:
    def choose(self, **_kwargs):
        raise AssertionError("the deterministic clinical contract must not invoke the planner")


def _forced_call_arguments(question: str) -> dict[str, dict[str, object]]:
    return {
        choice.name: choice.arguments
        for choice in _deterministic_tool_choices(question, BrandResolver())
    }


def test_disease_parenthetical_component_phrase_builds_authoritative_clinical_calls() -> None:
    # Given: a clinical and approval-review question names a disease before "(성분)".
    question = "뇌경색 질환(성분)의 임상·허가심사"

    # When: deterministic contract calls are built.
    calls = _forced_call_arguments(question)

    # Then: the two disease clinical sources are constructible without web fallback.
    assert calls == {
        "clinicaltrials_v2_search": {
            "query": "cerebral infarction",
            "query_type": "condition",
        },
        "mfds_clinical_trial_kr": {"query": "뇌경색", "query_type": "condition"},
    }


def test_legacy_disease_suffix_still_builds_condition_clinical_calls() -> None:
    # Given: the existing suffix-based disease extraction shape is used.
    question = "고지혈증 임상·허가심사"

    # When: deterministic contract calls are built.
    calls = _forced_call_arguments(question)

    # Then: existing disease aliases and condition query typing are preserved.
    assert calls == {
        "clinicaltrials_v2_search": {
            "query": "hyperlipidemia",
            "query_type": "condition",
        },
        "mfds_clinical_trial_kr": {"query": "고지혈증", "query_type": "condition"},
    }


def test_non_disease_parenthetical_component_phrase_is_not_overrecognized() -> None:
    # Given: a product-like noun uses the same "(성분)의 임상" surface.
    question = "자동차 질환(성분)의 임상·허가심사"

    # When: deterministic contract calls are built without a brand resolution.
    calls = _forced_call_arguments(question)

    # Then: the product noun is not reinterpreted as a disease condition.
    assert "clinicaltrials_v2_search" not in calls
    assert "mfds_clinical_trial_kr" not in calls


def test_non_disease_direct_clinical_phrase_is_not_overrecognized() -> None:
    calls = _forced_call_arguments("자동차 임상·허가심사")

    assert "clinicaltrials_v2_search" not in calls
    assert "mfds_clinical_trial_kr" not in calls


def test_unconstructible_permission_and_openfda_siblings_do_not_force_web_fallback() -> None:
    # Given: the disease phrase can build clinical calls but not permission/openfda args.
    question = "뇌경색 질환(성분)의 임상·허가심사"

    # When: deterministic contract calls are built.
    calls = _forced_call_arguments(question)

    # Then: unconstructible sibling tools are skipped without collapsing to generic web search.
    assert "mfds_permission_search" not in calls
    assert "openfda_label_search" not in calls
    assert "web_search" not in calls


def test_partial_clinical_result_discloses_unconstructible_sibling_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = "뇌경색 질환(성분)의 임상·허가심사"
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "OFF")
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_FORCE_CONTRACT_CALLS", "true")
    monkeypatch.setattr(
        integration_module.GenosToolChoiceProvider,
        "from_env",
        classmethod(lambda cls: _UnexpectedPlanner()),
    )

    payload = run_external_tool_agent(
        question,
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
    )

    assert payload["agent_loop_metrics"]["status"] == "partial"
    assert [call["tool"] for call in payload["tool_calls"]] == [
        "clinicaltrials_v2_search",
        "mfds_clinical_trial_kr",
    ]
    assert "식약처 허가정보" in payload["answer"]
    assert "FDA 라벨" in payload["answer"]
    assert "제품 또는 성분 식별자가 없어" in payload["answer"]
    assert "web_search" not in {call["tool"] for call in payload["tool_calls"]}
    assert {trace["status"] for trace in payload["agent_trace"]} >= {"not_constructible"}


def test_all_unconstructible_clinical_contract_returns_typed_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = "자동차 질환(성분)의 임상·허가심사"
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "OFF")
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_FORCE_CONTRACT_CALLS", "true")
    monkeypatch.setattr(
        integration_module.GenosToolChoiceProvider,
        "from_env",
        classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("planner must not load"))),
    )

    payload = run_external_tool_agent(
        question,
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
    )

    assert payload["agent_loop_metrics"]["status"] == "fallback"
    assert payload["router_diagnostics"]["fallback_code"] == "UNSUPPORTED_QUERY"
    assert payload["tool_calls"] == []
    assert payload["answer"].startswith("상태: 확인 불가")
    assert "질환 또는 제품 식별자를 결정론적으로 해소하지 못해" in payload["answer"]
    assert "web_search" not in payload["answer"]
