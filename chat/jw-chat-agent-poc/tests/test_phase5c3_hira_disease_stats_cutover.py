from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from jw_chat_agent_poc.orchestrator.typed_failure import (
    TypedFailureCode,
    normalize_typed_failure,
)
from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import SessionStore, compute_final_answer
from jw_chat_agent_poc.service.unified_router_cutover import (
    HIRA_DISEASE_STATS_CUTOVER_ENV,
    select_hira_disease_stats_cutover,
)


TARGET_QUESTIONS = (
    "D693 상병 환자수 최근 5년 알려줘",
    "D693 연령대별 환자수 알려줘",
    "D693 환자수 2021년부터 2024년까지 알려줘",
    "D693 환자수 최근 3년 알려줘",
    "H360 상병 환자수 알려줘",
    "고지혈증 환자수",
    "당뇨망막병증 환자수 알려줘",
    "당뇨망창병증 환자수 알려줘",
    "질병코드 H36.0 환자수 통계 알려줘",
    "질병코드 H360 환자수 통계 알려줘",
)
MIXED_QUESTION = "리바로 질병 환자수랑 최근 매출 한번에"
FIXTURES = Path(__file__).parent / "characterization" / "fixtures"


class _MarketScopeStub:
    def has_explicit_brand_anchor(self, question: str) -> bool:
        return "리바로" in question

    def has_explicit_named_market(self, question: str) -> bool:
        return "시장" in question


def _select(question: str, *, has_documents: bool = False):
    return select_hira_disease_stats_cutover(
        question=question,
        has_documents=has_documents,
        use_direct_agent_loop=True,
        market_scope_resolver=_MarketScopeStub(),
    )


def _stable_calls(payload: dict) -> list[dict]:
    return [
        {
            key: call.get(key)
            for key in ("tool", "source", "status", "summary_text", "render_data")
        }
        for call in payload.get("tool_calls", [])
    ]


@pytest.mark.parametrize("question", TARGET_QUESTIONS)
def test_hira_disease_stats_cutover_scope_is_exact(question: str) -> None:
    decision = _select(question)

    assert decision is not None
    assert decision.domain == "hira"
    assert decision.handler == "HIRA_DISEASE_PATIENT_STATS"
    assert decision.execution_mode.value == "deterministic"
    assert decision.capability == "HIRA_DISEASE_PATIENT_STATS"
    assert decision.requested_capabilities in (
        (),
        ("HIRA_DISEASE_PATIENT_STATS",),
    )


@pytest.mark.parametrize(
    "question",
    (
        "리바로 매출 알려줘",
        "리바로 급여기준 알려줘",
        "리바로 식약처 허가정보 알려줘",
        "리바로 임상시험 알려줘",
        MIXED_QUESTION,
    ),
)
def test_nontarget_and_mixed_questions_do_not_cut_over(question: str) -> None:
    assert _select(question) is None


def test_documents_keep_the_legacy_mixed_route() -> None:
    assert _select(TARGET_QUESTIONS[0], has_documents=True) is None


def test_adjudicated_scope_contains_ten_disease_stats_cases_and_excludes_17() -> None:
    payload = json.loads(
        (FIXTURES / "routing_mismatch_adjudication.v1.json").read_text(encoding="utf-8")
    )
    selected = {
        case["question"]
        for case in payload["cases"]
        if case["verdict"] == "CANONICAL_CORRECT" and _select(case["question"])
    }

    assert selected == set(TARGET_QUESTIONS)
    assert len(selected) == 10
    assert MIXED_QUESTION not in selected


def test_cutover_snapshot_contract_preserves_before_and_after() -> None:
    payload = json.loads(
        (FIXTURES / "hira_disease_stats_cutover.v1.json").read_text(encoding="utf-8")
    )

    assert payload["target_count"] == 10
    assert {case["question"] for case in payload["cases"]} == set(TARGET_QUESTIONS)
    assert all(case["before"]["route"] == {
        "domain": "market",
        "handler": "agent_loop",
        "mode": "agentic",
    } for case in payload["cases"])
    assert all(case["after"]["route"] == {
        "domain": "hira",
        "handler": "HIRA_DISEASE_PATIENT_STATS",
        "mode": "deterministic",
    } for case in payload["cases"])


def test_flag_off_does_not_import_the_cutover_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HIRA_DISEASE_STATS_CUTOVER_ENV, "0")
    real_import = builtins.__import__

    def guarded_import(name: str, *args, **kwargs):
        if name == "jw_chat_agent_poc.service.unified_router_cutover":
            raise AssertionError("disease-stat cutover imported while disabled")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert service_app._hira_disease_stats_cutover_decision(
        question=TARGET_QUESTIONS[0],
        has_documents=False,
        use_direct_agent_loop=True,
        market_scope_resolver=_MarketScopeStub(),
    ) is None


def test_enabled_cutover_consumes_canonical_route_before_legacy_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "question": TARGET_QUESTIONS[0],
        "answer": "공식 HIRA 질병통계",
        "tool_calls": [{"tool": "hira_disease_hospitalization_outpatient_stats", "status": "ok"}],
        "sources": ["hira"],
    }

    class _Canonical:
        domain = "hira"
        handler = "HIRA_DISEASE_PATIENT_STATS"
        execution_mode = type("Mode", (), {"value": "deterministic"})()

    monkeypatch.setattr(
        service_app,
        "_hira_disease_stats_cutover_decision",
        lambda **_kwargs: _Canonical(),
    )
    monkeypatch.setattr(
        service_app,
        "_answer_hira_disease_stats_cutover",
        lambda *_args, **_kwargs: expected,
    )
    monkeypatch.setattr(service_app, "observe_route_decision", lambda **_kwargs: None)
    monkeypatch.setattr(service_app, "observe_unified_market_shortcut_shadow", lambda **_kwargs: None)

    def forbidden_agent_factory(**_kwargs):
        raise AssertionError("legacy agent must not run for disease-stat cutover")

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
    assert result["router_diagnostics"]["canonical_router_cutover"] == {
        "domain": "hira",
        "handler": "HIRA_DISEASE_PATIENT_STATS",
        "mode": "deterministic",
    }


def test_cutover_execution_failure_falls_back_to_legacy_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"question": TARGET_QUESTIONS[0], "answer": "legacy", "tool_calls": []}

    class _Canonical:
        domain = "hira"
        handler = "HIRA_DISEASE_PATIENT_STATS"
        execution_mode = type("Mode", (), {"value": "deterministic"})()

    class _Agent:
        def answer(self, question: str, documents, **_kwargs):
            return expected

    monkeypatch.setattr(
        service_app,
        "_hira_disease_stats_cutover_decision",
        lambda **_kwargs: _Canonical(),
    )
    monkeypatch.setattr(
        service_app,
        "_answer_hira_disease_stats_cutover",
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
def test_target_before_after_keeps_authoritative_result_and_changes_route_owner(
    question: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payloads = []
    for enabled in ("0", "1"):
        monkeypatch.setenv(HIRA_DISEASE_STATS_CUTOVER_ENV, enabled)
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
    assert _stable_calls(before) == _stable_calls(after)
    assert "canonical_router_cutover" not in before["router_diagnostics"]
    assert after["router_diagnostics"]["canonical_router_cutover"] == {
        "domain": "hira",
        "handler": "HIRA_DISEASE_PATIENT_STATS",
        "mode": "deterministic",
    }
    capsys.readouterr()


@pytest.mark.parametrize(
    "question",
    (
        "리바로 매출 알려줘",
        "리바로 급여기준 알려줘",
        "리바로 식약처 허가정보 알려줘",
        "리바로 임상시험 알려줘",
        MIXED_QUESTION,
    ),
)
def test_nontarget_answers_and_routes_are_unchanged(
    question: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payloads = []
    for enabled in ("0", "1"):
        monkeypatch.setenv(HIRA_DISEASE_STATS_CUTOVER_ENV, enabled)
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
    assert _stable_calls(before) == _stable_calls(after)
    assert _select(question) is None
    assert before.get("router_diagnostics", {}).get("canonical_router_cutover") == (
        after.get("router_diagnostics", {}).get("canonical_router_cutover")
    )
    assert after.get("router_diagnostics", {}).get("canonical_router_cutover", {}).get(
        "handler"
    ) != "HIRA_DISEASE_PATIENT_STATS"
    capsys.readouterr()


def test_fb04_absent_code_remains_actionable_after_cutover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(HIRA_DISEASE_STATS_CUTOVER_ENV, "1")
    question = "당뇨망창병증 환자수 알려줘"
    result = service_app._answer_existing_without_pending(
        _MarketScopeStub(),
        service_app._default_agent_factory,
        "conversation",
        question,
        "fixture",
        None,
        SessionStore(),
        use_direct_agent_loop=True,
    )
    normalized = normalize_typed_failure(result)
    final = compute_final_answer(question, result, "phase5c3-fb04")

    assert normalized is not None
    assert normalized.code is TypedFailureCode.DISEASE_CODE_ABSENT
    assert "https://opendata.hira.or.kr/" in final.text
    assert "검색어: 당뇨망창병증" in final.text
    assert "상병코드를 직접" in final.text
    assert result["router_diagnostics"]["canonical_router_cutover"]["handler"] == (
        "HIRA_DISEASE_PATIENT_STATS"
    )
