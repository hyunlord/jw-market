from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import SessionStore
from jw_chat_agent_poc.service import unified_router_cutover as cutover
from jw_chat_agent_poc.tool_use.routing_v4_rules import classify_question


TARGET_CASES = (
    ("리바로 식약처 허가정보 알려줘", "MFDS_BASIC_PRODUCT_INFO"),
    ("리바로 효능효과 알려줘", "MFDS_PERMISSION_DETAIL_FIELDS"),
    ("아일리아 허가정보 알려줘", "MFDS_BASIC_PRODUCT_INFO"),
)
FB02_QUESTION = "뇌경색 관련 임상시험이랑 허가 현황 알려줘"
MIXED_QUESTION = "리바로 질병 환자수랑 최근 매출 한번에"
FIXTURES = Path(__file__).parent / "characterization" / "fixtures"


class _MarketScopeStub:
    def has_explicit_brand_anchor(self, question: str) -> bool:
        return any(brand in question for brand in ("리바로", "아일리아"))

    def has_explicit_named_market(self, question: str) -> bool:
        return "시장" in question


def _select(question: str, *, has_documents: bool = False):
    return cutover.select_mfds_cutover(
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


@pytest.mark.parametrize(("question", "capability"), TARGET_CASES)
def test_mfds_cutover_scope_is_exact(question: str, capability: str) -> None:
    decision = _select(question)

    assert decision is not None
    assert decision.domain == "regulatory"
    assert decision.handler == capability
    assert decision.capability == capability
    assert decision.execution_mode.value == "deterministic"
    assert decision.requested_capabilities in ((), (capability,))


@pytest.mark.parametrize(
    "question",
    (
        "리바로 매출 알려줘",
        "리바로 급여기준 알려줘",
        "리바로 질병 환자수 알려줘",
        "리바로 임상시험 알려줘",
        MIXED_QUESTION,
        FB02_QUESTION,
    ),
)
def test_nontarget_questions_do_not_cut_over(question: str) -> None:
    assert _select(question) is None


def test_documents_keep_the_legacy_mixed_route() -> None:
    assert _select(TARGET_CASES[0][0], has_documents=True) is None


def test_authoritative_scope_contains_exactly_three_cases_and_excludes_fb02() -> None:
    payload = json.loads(
        (FIXTURES / "routing_mismatch_adjudication.v1.json").read_text(encoding="utf-8")
    )
    selected = {
        case["question"]: _select(case["question"]).capability
        for case in payload["cases"]
        if case["verdict"] == "CANONICAL_CORRECT" and _select(case["question"])
    }

    assert selected == dict(TARGET_CASES)
    assert len(selected) == 3
    assert FB02_QUESTION not in selected


def test_cutover_snapshot_contract_preserves_before_and_after() -> None:
    payload = json.loads((FIXTURES / "mfds_cutover.v1.json").read_text(encoding="utf-8"))

    assert payload["target_count"] == 3
    assert {(case["question"], case["capability"]) for case in payload["cases"]} == set(
        TARGET_CASES
    )
    assert all(
        case["before"]["route"]
        == {"domain": "market", "handler": "agent_loop", "mode": "agentic"}
        for case in payload["cases"]
    )
    assert all(
        case["after"]["route"]
        == {
            "domain": "regulatory",
            "handler": case["capability"],
            "mode": "deterministic",
        }
        for case in payload["cases"]
    )


def test_basic_and_detail_capabilities_remain_distinct() -> None:
    basic = _select("리바로 식약처 허가정보 알려줘")
    detail = _select("리바로 효능효과 알려줘")

    assert basic is not None and detail is not None
    assert basic.capability == basic.handler == "MFDS_BASIC_PRODUCT_INFO"
    assert detail.capability == detail.handler == "MFDS_PERMISSION_DETAIL_FIELDS"
    assert basic.capability != detail.capability


def test_flag_off_does_not_import_the_cutover_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cutover.MFDS_CUTOVER_ENV, "0")
    real_import = builtins.__import__

    def guarded_import(name: str, *args, **kwargs):
        if name == "jw_chat_agent_poc.service.unified_router_cutover":
            raise AssertionError("MFDS cutover imported while disabled")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert service_app._mfds_cutover_decision(
        question=TARGET_CASES[0][0],
        has_documents=False,
        use_direct_agent_loop=True,
        market_scope_resolver=_MarketScopeStub(),
    ) is None


def test_enabled_cutover_consumes_canonical_route_before_legacy_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "question": TARGET_CASES[1][0],
        "answer": "식약처 공식 효능효과",
        "tool_calls": [{"tool": "mfds_permission_search", "status": "ok"}],
        "sources": ["nedrug_mcp"],
    }

    class _Canonical:
        domain = "regulatory"
        handler = "MFDS_PERMISSION_DETAIL_FIELDS"
        execution_mode = type("Mode", (), {"value": "deterministic"})()

    monkeypatch.setattr(service_app, "_mfds_cutover_decision", lambda **_kwargs: _Canonical())
    monkeypatch.setattr(service_app, "_answer_mfds_cutover", lambda *_args, **_kwargs: expected)
    monkeypatch.setattr(service_app, "observe_route_decision", lambda **_kwargs: None)
    monkeypatch.setattr(service_app, "observe_unified_market_shortcut_shadow", lambda **_kwargs: None)

    def forbidden_agent_factory(**_kwargs):
        raise AssertionError("legacy agent must not run for MFDS cutover")

    result = service_app._answer_existing_without_pending(
        _MarketScopeStub(),
        forbidden_agent_factory,
        "conversation",
        TARGET_CASES[1][0],
        "fixture",
        None,
        SessionStore(),
        use_direct_agent_loop=True,
    )

    assert result is expected
    assert result["router_diagnostics"]["canonical_router_cutover"] == {
        "domain": "regulatory",
        "handler": "MFDS_PERMISSION_DETAIL_FIELDS",
        "mode": "deterministic",
    }


def test_cutover_execution_failure_falls_back_to_legacy_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"question": TARGET_CASES[0][0], "answer": "legacy", "tool_calls": []}

    class _Canonical:
        domain = "regulatory"
        handler = "MFDS_BASIC_PRODUCT_INFO"
        execution_mode = type("Mode", (), {"value": "deterministic"})()

    class _Agent:
        def answer(self, question: str, documents, **_kwargs):
            return expected

    monkeypatch.setattr(service_app, "_mfds_cutover_decision", lambda **_kwargs: _Canonical())
    monkeypatch.setattr(service_app, "_answer_mfds_cutover", lambda *_args, **_kwargs: None)

    result = service_app._answer_existing_without_pending(
        _MarketScopeStub(),
        lambda **_kwargs: _Agent(),
        "conversation",
        TARGET_CASES[0][0],
        "fixture",
        None,
        SessionStore(),
        use_direct_agent_loop=True,
    )

    assert result is expected


@pytest.mark.parametrize(("question", "capability"), TARGET_CASES)
def test_target_before_after_keeps_authoritative_result_and_changes_route_owner(
    question: str,
    capability: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payloads = []
    for enabled in ("0", "1"):
        monkeypatch.setenv(cutover.MFDS_CUTOVER_ENV, enabled)
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
    assert "canonical_router_cutover" not in before["router_diagnostics"]
    assert after["router_diagnostics"]["canonical_router_cutover"] == {
        "domain": "regulatory",
        "handler": capability,
        "mode": "deterministic",
    }
    assert after["tool_calls"][0]["tool"] == "mfds_permission_search"
    if capability == "MFDS_PERMISSION_DETAIL_FIELDS":
        assert before["answer"] == after["answer"]
        assert _stable_calls(before) == _stable_calls(after)
    elif question.startswith("리바로"):
        assert "리바로정1밀리그램" in after["answer"]
        assert "리바로정2밀리그램" in after["answer"]
        assert "Pitavastatin Calcium Hydrate" in after["answer"]
        assert after["sources"] == ["식약처 의약품 정보"]
    else:
        assert before["answer"] == after["answer"]
        assert "canonical 제품군 근거를 찾지 못했습니다" in after["answer"]
        assert after["tool_calls"][0]["status"] == "error"
        assert after["sources"] == []
    capsys.readouterr()


@pytest.mark.parametrize(
    "question",
    (
        "리바로 매출 알려줘",
        "리바로 급여기준 알려줘",
        "리바로 질병 환자수 알려줘",
        "리바로 임상시험 알려줘",
        MIXED_QUESTION,
        FB02_QUESTION,
    ),
)
def test_nontarget_answers_and_routes_are_unchanged(
    question: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payloads = []
    for enabled in ("0", "1"):
        monkeypatch.setenv(cutover.MFDS_CUTOVER_ENV, enabled)
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
    capsys.readouterr()


def test_fb02_permission_facets_remain_preserved() -> None:
    classification = classify_question(FB02_QUESTION)

    assert classification.requested_facets == ("clinical", "permission")
    assert tuple(facet.facet for facet in classification.unresolvable_facets) == (
        "permission",
    )
    assert _select(FB02_QUESTION) is None
