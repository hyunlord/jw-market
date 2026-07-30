from __future__ import annotations

import pytest

from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import SessionStore
from jw_chat_agent_poc.service.context_scope import (
    ContextScope,
    has_file_reference,
    resolve_context_scope,
)
from jw_chat_agent_poc.tool_use.integration import _deterministic_tool_choices
from jw_chat_agent_poc.tool_use.integration import run_external_tool_agent
from jw_chat_agent_poc.tool_use.routing_v4_rules import classify_question
from jw_chat_agent_poc.tools.external import ExternalApiClient, ExternalCall


class _NoMarketResolver:
    @staticmethod
    def has_explicit_brand_anchor(_question: str) -> bool:
        return False

    @staticmethod
    def has_explicit_anchor(_question: str) -> bool:
        return False


@pytest.mark.parametrize(
    ("question", "capability"),
    (
        ("리바로 효능효과 알려줘", "MFDS_PERMISSION_DETAIL_FIELDS"),
        ("아일리아 허가정보 알려줘", "MFDS_BASIC_PRODUCT_INFO"),
    ),
)
def test_regulatory_domain_precedes_market_routing(
    question: str,
    capability: str,
) -> None:
    classification = classify_question(question)

    assert classification.source_domain == "regulatory"
    assert classification.requested_capability == capability
    assert [
        (choice.name, choice.arguments)
        for choice in _deterministic_tool_choices(question, BrandResolver())
    ] == [("mfds_permission_search", {"brand": question.split()[0]})]


@pytest.mark.parametrize(
    "question",
    (
        "첨부한 PPT에서 시장 규모 수치만 찾아줘",
        "올린 워드 문서의 표를 정리해줘",
    ),
)
def test_presentation_and_word_requests_use_file_scope_without_an_upload(
    question: str,
) -> None:
    assert has_file_reference(question)
    assert (
        resolve_context_scope(
            question,
            has_active_file=False,
            has_market_intent=True,
            has_market_anchor=False,
        )
        is ContextScope.FILE
    )


@pytest.mark.parametrize(
    "question",
    (
        "첨부한 PPT에서 시장 규모 수치만 찾아줘",
        "올린 워드 문서의 표를 정리해줘",
    ),
)
def test_file_domain_reaches_existing_missing_upload_path_without_market_tools(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
) -> None:
    monkeypatch.setattr(service_app, "_observe_query_spec", lambda *_args: None)
    monkeypatch.setattr(
        service_app,
        "_delegated_file_context",
        lambda *_args: (None, (), False, "", ()),
    )

    def fail_factory(*, external_mode: str = "live") -> object:
        raise AssertionError(f"file-domain request entered market agent in {external_mode}")

    item = service_app._answer_question(
        SessionStore(),
        _NoMarketResolver(),
        fail_factory,
        question,
        "fixture",
        None,
    )

    result = item["result"]
    assert result["context_scope"] == ContextScope.FILE.value
    assert result["tool_calls"] == []
    assert result["router_diagnostics"]["mode"] == "file_context_scope_lock"


@pytest.mark.parametrize(
    ("question", "source_domain", "capability"),
    (
        ("리바로 용법용량 알려줘", "regulatory", "MFDS_PERMISSION_DETAIL_FIELDS"),
        ("리바로 사용상 주의사항 알려줘", "regulatory", "MFDS_PERMISSION_DETAIL_FIELDS"),
        ("리바로 보험인정기준 알려줘", "hira", "HIRA_REIMBURSEMENT_CRITERIA"),
        ("리바로 임상시험 알려줘", "clinical_trials", "CLINICAL_TRIAL_SEARCH"),
    ),
)
def test_domain_first_synonyms_are_classified_before_market_intents(
    question: str,
    source_domain: str,
    capability: str,
) -> None:
    classification = classify_question(question)

    assert classification.source_domain == source_domain
    assert classification.requested_capability == capability


def test_generic_business_effect_is_not_misclassified_as_drug_efficacy() -> None:
    classification = classify_question("영업활동 효과 알려줘")

    assert classification.source_domain == "unresolved"


@pytest.mark.parametrize(
    "question",
    (
        "리바로 효능효과 알려줘",
        "아일리아 허가정보 알려줘",
    ),
)
def test_regulatory_domain_calls_nedrug_in_legacy_mode_without_force_flag(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
) -> None:
    external = ExternalApiClient(mode="fixture")
    expected_brand = question.split()[0]

    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "OFF")
    monkeypatch.delenv("CHAT_EXTERNAL_TOOL_FORCE_CONTRACT_CALLS", raising=False)
    monkeypatch.setattr(
        external,
        "mfds_permission_search",
        lambda brand: ExternalCall(
            tool="mfds_permission_search",
            source="external_api",
            status="ok",
            summary_text=f"{brand} 허가 품목 1건",
            render_data={
                "items": [{"ITEM_SEQ": "item-1", "ITEM_NAME": expected_brand}],
            },
        ),
    )
    monkeypatch.setattr(
        external,
        "mfds_permission_detail",
        lambda item_seq: ExternalCall(
            tool="mfds_permission_detail",
            source="external_api",
            status="ok",
            summary_text=f"{item_seq} 허가 상세",
            render_data={
                "items": [
                    {
                        "ITEM_SEQ": item_seq,
                        "ITEM_NAME": expected_brand,
                        "EE_DOC_DATA": "검증된 효능효과 원문",
                    }
                ],
            },
        ),
    )

    payload = run_external_tool_agent(
        question,
        resolver=BrandResolver(),
        external=external,
    )

    assert [call["tool"] for call in payload["tool_calls"]] == ["mfds_permission_search"]
