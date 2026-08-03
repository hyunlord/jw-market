from __future__ import annotations

import pytest

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import SessionStore
from jw_chat_agent_poc.service import unified_router_cutover as cutover


NCT_DETAIL_CASES = (
    "NCT05151731 선정제외기준 알려줘",
    "NCT05151731 시험 디자인 알려줘",
    "NCT05151731 임상시험 상태 알려줘",
)
TARGET_CASES = tuple(
    [(question, "CLINICAL_TRIAL_NCT_DETAIL_FIELDS") for question in NCT_DETAIL_CASES]
    + [
        (question, "CLINICAL_TRIAL_SEARCH")
        for question in (
            "뇌경색 관련 임상시험이랑 허가 현황 알려줘",
            "뇌경색 관련 진행 중인 임상시험 알려줘",
            "리바로 임상시험 알려줘",
        )
    ]
)
NONTARGET_CASES = (
    "리바로 매출 알려줘",
    "리바로 급여기준 알려줘",
    "리바로 질병 환자수 알려줘",
    "리바로 식약처 허가정보 알려줘",
    "리바로 효능효과 알려줘",
    "리바로 질병 환자수랑 최근 매출 한번에",
)


class _MarketScopeStub:
    def has_explicit_brand_anchor(self, question: str) -> bool:
        return "리바로" in question

    def has_explicit_named_market(self, question: str) -> bool:
        return "시장" in question


def _stable_calls(payload: dict) -> list[dict]:
    return [
        {
            key: call.get(key)
            for key in ("tool", "source", "status", "summary_text", "render_data")
        }
        for call in payload.get("tool_calls", [])
    ]


@pytest.mark.parametrize(("question", "capability"), TARGET_CASES)
def test_target_answers_preserve_quality_while_route_owner_changes(
    question: str,
    capability: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payloads = []
    for enabled in ("0", "1"):
        monkeypatch.setenv(cutover.CLINICAL_TRIALS_CUTOVER_ENV, enabled)
        monkeypatch.setenv(cutover.CLINICAL_FB02_CUTOVER_ENV, "1")
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
    assert before.get("router_diagnostics", {}).get("canonical_router_cutover") is None
    assert after["router_diagnostics"]["canonical_router_cutover"] == {
        "domain": "clinical_trials",
        "handler": capability,
        "mode": "deterministic" if capability.endswith("DETAIL_FIELDS") else "agentic",
    }
    capsys.readouterr()


def test_enabled_cutover_consumes_canonical_route_before_legacy_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"question": NCT_DETAIL_CASES[0], "answer": "clinical", "tool_calls": []}

    class _Canonical:
        domain = "clinical_trials"
        handler = "CLINICAL_TRIAL_NCT_DETAIL_FIELDS"
        execution_mode = type("Mode", (), {"value": "deterministic"})()

    monkeypatch.setattr(
        service_app, "_clinical_trials_cutover_decision", lambda **_kwargs: _Canonical()
    )
    monkeypatch.setattr(
        service_app, "_answer_clinical_trials_cutover", lambda *_args, **_kwargs: expected
    )
    monkeypatch.setattr(service_app, "observe_route_decision", lambda **_kwargs: None)
    monkeypatch.setattr(service_app, "observe_unified_market_shortcut_shadow", lambda **_kwargs: None)

    def forbidden_agent_factory(**_kwargs):
        raise AssertionError("legacy branch must not construct a second agent")

    result = service_app._answer_existing_without_pending(
        _MarketScopeStub(),
        forbidden_agent_factory,
        "conversation",
        NCT_DETAIL_CASES[0],
        "fixture",
        None,
        SessionStore(),
        use_direct_agent_loop=True,
    )

    assert result is expected
    assert result["router_diagnostics"]["canonical_router_cutover"] == {
        "domain": "clinical_trials",
        "handler": "CLINICAL_TRIAL_NCT_DETAIL_FIELDS",
        "mode": "deterministic",
    }


def test_cutover_execution_failure_falls_back_to_legacy_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"question": NCT_DETAIL_CASES[0], "answer": "legacy", "tool_calls": []}

    class _Canonical:
        domain = "clinical_trials"
        handler = "CLINICAL_TRIAL_NCT_DETAIL_FIELDS"
        execution_mode = type("Mode", (), {"value": "deterministic"})()

    class _Agent:
        def answer(self, question: str, documents, **_kwargs):
            return expected

    monkeypatch.setattr(
        service_app, "_clinical_trials_cutover_decision", lambda **_kwargs: _Canonical()
    )
    monkeypatch.setattr(
        service_app, "_answer_clinical_trials_cutover", lambda *_args, **_kwargs: None
    )

    result = service_app._answer_existing_without_pending(
        _MarketScopeStub(),
        lambda **_kwargs: _Agent(),
        "conversation",
        NCT_DETAIL_CASES[0],
        "fixture",
        None,
        SessionStore(),
        use_direct_agent_loop=True,
    )
    assert result is expected


@pytest.mark.parametrize("question", NONTARGET_CASES)
def test_nontarget_answers_and_routes_are_unchanged(
    question: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payloads = []
    for enabled in ("0", "1"):
        monkeypatch.setenv(cutover.CLINICAL_TRIALS_CUTOVER_ENV, enabled)
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
    assert before.get("router_diagnostics", {}).get("canonical_router_cutover") == (
        after.get("router_diagnostics", {}).get("canonical_router_cutover")
    )
    capsys.readouterr()
