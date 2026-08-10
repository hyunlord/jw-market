from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import SessionStore
from jw_chat_agent_poc.service.unified_router_cutover import (
    HIRA_REIMBURSEMENT_CUTOVER_ENV,
    select_hira_reimbursement_cutover,
)


TARGET_QUESTIONS = (
    "리바로 급여기준 알려줘",
    "아일리아 급여기준 알려줘",
    "악템라 급여기준 알려줘",
    "헴리브라 급여기준 알려줘",
)
FIXTURES = Path(__file__).parent / "characterization" / "fixtures"


class _MarketScopeStub:
    def has_explicit_brand_anchor(self, question: str) -> bool:
        return any(brand in question for brand in ("리바로", "아일리아", "악템라", "헴리브라"))

    def has_explicit_named_market(self, question: str) -> bool:
        return "시장" in question


@pytest.mark.parametrize("question", TARGET_QUESTIONS)
def test_hira_reimbursement_cutover_scope_is_exact(question: str) -> None:
    decision = select_hira_reimbursement_cutover(
        question=question,
        has_documents=False,
        use_direct_agent_loop=True,
        market_scope_resolver=_MarketScopeStub(),
    )

    assert decision is not None
    assert decision.domain == "hira"
    assert decision.handler == "HIRA_REIMBURSEMENT_CRITERIA"
    assert decision.execution_mode.value == "deterministic"
    assert decision.capability == "HIRA_REIMBURSEMENT_CRITERIA"


@pytest.mark.parametrize(
    "question",
    (
        "리바로 매출 알려줘",
        "리바로 질병 환자수 알려줘",
        "리바로 식약처 허가정보 알려줘",
        "리바로 임상시험 알려줘",
    ),
)
def test_non_reimbursement_questions_do_not_cut_over(question: str) -> None:
    assert (
        select_hira_reimbursement_cutover(
            question=question,
            has_documents=False,
            use_direct_agent_loop=True,
            market_scope_resolver=_MarketScopeStub(),
        )
        is None
    )


def test_documents_keep_the_legacy_mixed_route() -> None:
    assert (
        select_hira_reimbursement_cutover(
            question=TARGET_QUESTIONS[0],
            has_documents=True,
            use_direct_agent_loop=True,
            market_scope_resolver=_MarketScopeStub(),
        )
        is None
    )


def test_corpus_cutover_changes_exactly_four_of_128_routes() -> None:
    corpus = json.loads((FIXTURES / "corpus.v1.json").read_text(encoding="utf-8"))
    selected = {
        case["question"]
        for case in corpus["cases"]
        if select_hira_reimbursement_cutover(
            question=case["question"],
            has_documents=False,
            use_direct_agent_loop=True,
            market_scope_resolver=_MarketScopeStub(),
        )
        is not None
    }

    assert len(corpus["cases"]) == 128
    assert selected == set(TARGET_QUESTIONS)
    assert len(corpus["cases"]) - len(selected) == 124


def test_cutover_snapshot_contract_preserves_before_and_after() -> None:
    payload = json.loads(
        (FIXTURES / "hira_reimbursement_cutover.v1.json").read_text(encoding="utf-8")
    )

    assert payload["target_count"] == 4
    assert {case["question"] for case in payload["cases"]} == set(TARGET_QUESTIONS)
    assert all(case["before"]["answer_snapshots"] for case in payload["cases"])
    assert all(case["after"]["route"] == {
        "domain": "hira",
        "handler": "HIRA_REIMBURSEMENT_CRITERIA",
        "mode": "deterministic",
    } for case in payload["cases"])
    assert all(case["after"]["answer_contract"] == "same authoritative HIRA execution" for case in payload["cases"])


def test_flag_off_does_not_import_the_cutover_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HIRA_REIMBURSEMENT_CUTOVER_ENV, "0")
    real_import = builtins.__import__

    def guarded_import(name: str, *args, **kwargs):
        if name == "jw_chat_agent_poc.service.unified_router_cutover":
            raise AssertionError("cutover consumer imported while disabled")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert (
        service_app._hira_reimbursement_cutover_decision(
            question=TARGET_QUESTIONS[0],
            has_documents=False,
            use_direct_agent_loop=True,
            market_scope_resolver=_MarketScopeStub(),
        )
        is None
    )


def test_enabled_cutover_consumes_canonical_route_before_legacy_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "question": TARGET_QUESTIONS[0],
        "answer": "공식 HIRA 급여기준",
        "tool_calls": [{"tool": "hira_reimbursement_criteria", "status": "ok"}],
        "sources": ["hira"],
    }

    class _Canonical:
        domain = "hira"
        handler = "HIRA_REIMBURSEMENT_CRITERIA"
        execution_mode = type("Mode", (), {"value": "deterministic"})()

    monkeypatch.setattr(
        service_app,
        "_hira_reimbursement_cutover_decision",
        lambda **_kwargs: _Canonical(),
    )
    monkeypatch.setattr(
        service_app,
        "_answer_hira_reimbursement_cutover",
        lambda *_args, **_kwargs: expected,
    )
    observed: list[dict] = []
    monkeypatch.setattr(service_app, "observe_route_decision", lambda **kwargs: observed.append(kwargs))
    monkeypatch.setattr(service_app, "observe_unified_market_shortcut_shadow", lambda **_kwargs: None)

    def forbidden_agent_factory(**_kwargs):
        raise AssertionError("legacy agent must not run for the cutover capability")

    result = service_app._answer_existing_without_pending(
        _MarketScopeStub(),
        forbidden_agent_factory,
        "conversation",
        TARGET_QUESTIONS[0],
        "fixture",
        None,
        SessionStore(),
        use_direct_agent_loop=True,
    )

    assert result is expected
    assert observed[-1]["domain"] == "hira"
    assert observed[-1]["handler"] == "HIRA_REIMBURSEMENT_CRITERIA"
    assert result["router_diagnostics"]["canonical_router_cutover"] == {
        "domain": "hira",
        "handler": "HIRA_REIMBURSEMENT_CRITERIA",
        "mode": "deterministic",
    }


def test_disabled_cutover_keeps_the_legacy_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HIRA_REIMBURSEMENT_CUTOVER_ENV, "0")
    expected = {"question": TARGET_QUESTIONS[0], "answer": "legacy", "tool_calls": []}

    class _Agent:
        def answer(self, question: str, documents, **_kwargs):
            assert question == TARGET_QUESTIONS[0]
            assert documents is None
            return expected

    result = service_app._answer_existing_without_pending(
        _MarketScopeStub(),
        lambda **_kwargs: _Agent(),
        "conversation",
        TARGET_QUESTIONS[0],
        "fixture",
        None,
        SessionStore(),
        use_direct_agent_loop=True,
    )

    assert result is expected


def test_cutover_execution_failure_falls_back_to_legacy_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"question": TARGET_QUESTIONS[0], "answer": "legacy", "tool_calls": []}

    class _Canonical:
        domain = "hira"
        handler = "HIRA_REIMBURSEMENT_CRITERIA"
        execution_mode = type("Mode", (), {"value": "deterministic"})()

    class _Agent:
        def answer(self, question: str, documents, **_kwargs):
            assert question == TARGET_QUESTIONS[0]
            assert documents is None
            return expected

    monkeypatch.setattr(
        service_app,
        "_hira_reimbursement_cutover_decision",
        lambda **_kwargs: _Canonical(),
    )
    monkeypatch.setattr(
        service_app,
        "_answer_hira_reimbursement_cutover",
        lambda *_args, **_kwargs: None,
    )

    result = service_app._answer_existing_without_pending(
        _MarketScopeStub(),
        lambda **_kwargs: _Agent(),
        "conversation",
        TARGET_QUESTIONS[0],
        "fixture",
        None,
        SessionStore(),
        use_direct_agent_loop=True,
    )

    assert result is expected


@pytest.mark.parametrize("question", TARGET_QUESTIONS)
def test_target_before_after_uses_same_authoritative_answer_with_new_route_owner(
    question: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "false")

    payloads = []
    for enabled in ("0", "1"):
        monkeypatch.setenv(HIRA_REIMBURSEMENT_CUTOVER_ENV, enabled)
        payloads.append(
            service_app._answer_existing_without_pending(
                _MarketScopeStub(),
                service_app._default_agent_factory,
                "conversation",
                question,
                "fixture",
                None,
                SessionStore(),
                use_direct_agent_loop=True,
            )
        )

    before, after = payloads
    assert before["answer"] == after["answer"]
    before_calls = [
        {key: value for key, value in call.items() if key != "queried_at_utc"}
        for call in before["tool_calls"]
    ]
    after_calls = [
        {key: value for key, value in call.items() if key != "queried_at_utc"}
        for call in after["tool_calls"]
    ]
    assert before_calls == after_calls
    assert "canonical_router_cutover" not in before["router_diagnostics"]
    assert after["router_diagnostics"]["canonical_router_cutover"] == {
        "domain": "hira",
        "handler": "HIRA_REIMBURSEMENT_CRITERIA",
        "mode": "deterministic",
    }
    assert [call["tool"] for call in after["tool_calls"]] == [
        "hira_reimbursement_criteria",
        "web_search",
    ]
    assert after["agent_loop_metrics"]["status"] == "typed_stop"
    assert "확인 가능한 기록을 찾지 못했습니다" in after["answer"]
    capsys.readouterr()
