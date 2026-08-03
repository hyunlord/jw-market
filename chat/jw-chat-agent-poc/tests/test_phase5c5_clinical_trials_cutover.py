from __future__ import annotations

import builtins
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import SessionStore
from jw_chat_agent_poc.service import unified_router_cutover as cutover


NCT_DETAIL_CASES = (
    "NCT05151731 선정제외기준 알려줘",
    "NCT05151731 시험 디자인 알려줘",
    "NCT05151731 임상시험 상태 알려줘",
)
FB02 = "뇌경색 관련 임상시험이랑 허가 현황 알려줘"
CLINICAL_SEARCH_CASES = (
    FB02,
    "뇌경색 관련 진행 중인 임상시험 알려줘",
    "리바로 임상시험 알려줘",
)
TARGET_CASES = tuple(
    [(question, "CLINICAL_TRIAL_NCT_DETAIL_FIELDS") for question in NCT_DETAIL_CASES]
    + [(question, "CLINICAL_TRIAL_SEARCH") for question in CLINICAL_SEARCH_CASES]
)
NONTARGET_CASES = (
    "리바로 매출 알려줘",
    "리바로 급여기준 알려줘",
    "리바로 질병 환자수 알려줘",
    "리바로 식약처 허가정보 알려줘",
    "리바로 효능효과 알려줘",
    "리바로 질병 환자수랑 최근 매출 한번에",
)
FIXTURES = Path(__file__).parent / "characterization" / "fixtures"


class _MarketScopeStub:
    def has_explicit_brand_anchor(self, question: str) -> bool:
        return "리바로" in question

    def has_explicit_named_market(self, question: str) -> bool:
        return "시장" in question


def _select(question: str, *, has_documents: bool = False):
    return cutover.select_clinical_trials_cutover(
        question=question,
        has_documents=has_documents,
        use_direct_agent_loop=True,
        market_scope_resolver=_MarketScopeStub(),
    )


@pytest.mark.parametrize(("question", "capability"), TARGET_CASES)
def test_clinical_cutover_scope_is_exact(question: str, capability: str) -> None:
    decision = _select(question)

    assert decision is not None
    assert decision.domain == "clinical_trials"
    assert decision.handler == capability
    assert decision.capability == capability
    expected_mode = "deterministic" if capability.endswith("DETAIL_FIELDS") else "agentic"
    assert decision.execution_mode.value == expected_mode


@pytest.mark.parametrize("question", NONTARGET_CASES)
def test_nontarget_questions_do_not_cut_over(question: str) -> None:
    assert _select(question) is None


def test_documents_keep_the_legacy_mixed_route() -> None:
    assert _select(NCT_DETAIL_CASES[0], has_documents=True) is None


def test_authoritative_scope_contains_six_cases_and_includes_fb02() -> None:
    payload = json.loads(
        (FIXTURES / "routing_mismatch_adjudication.v1.json").read_text(encoding="utf-8")
    )
    selected = {
        case["question"]: _select(case["question"]).capability
        for case in payload["cases"]
        if case["verdict"] == "CANONICAL_CORRECT" and _select(case["question"])
    }

    assert selected == dict(TARGET_CASES)
    assert len(selected) == 6
    assert FB02 in selected


def test_additive_snapshot_preserves_before_and_after() -> None:
    payload = json.loads(
        (FIXTURES / "clinical_trials_cutover.v1.json").read_text(encoding="utf-8")
    )

    assert payload["target_count"] == 6
    assert {(case["question"], case["capability"]) for case in payload["cases"]} == set(
        TARGET_CASES
    )
    assert all(
        case["before"]["route"]
        == {"domain": "market", "handler": "agent_loop", "mode": "agentic"}
        for case in payload["cases"]
    )


def test_nct_detail_and_search_capabilities_remain_distinct() -> None:
    detail = _select(NCT_DETAIL_CASES[0])
    search = _select(CLINICAL_SEARCH_CASES[1])

    assert detail is not None and search is not None
    assert detail.capability == "CLINICAL_TRIAL_NCT_DETAIL_FIELDS"
    assert search.capability == "CLINICAL_TRIAL_SEARCH"
    assert detail.capability != search.capability
    assert detail.execution_mode.value == "deterministic"
    assert search.execution_mode.value == "agentic"


def test_main_flag_off_does_not_import_cutover_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cutover.CLINICAL_TRIALS_CUTOVER_ENV, "0")
    real_import = builtins.__import__

    def guarded_import(name: str, *args, **kwargs):
        if name == "jw_chat_agent_poc.service.unified_router_cutover":
            raise AssertionError("clinical cutover imported while disabled")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert service_app._clinical_trials_cutover_decision(
        question=NCT_DETAIL_CASES[0],
        has_documents=False,
        use_direct_agent_loop=True,
        market_scope_resolver=_MarketScopeStub(),
    ) is None


def test_main_flag_off_cold_import_does_not_load_cutover_consumer() -> None:
    project_root = Path(__file__).resolve().parents[1]
    module_name = "jw_chat_agent_poc.service.unified_router_cutover"
    script = f"""
import sys

class BlockCutoverModule:
    def find_spec(self, fullname, path, target=None):
        if fullname == {module_name!r}:
            raise RuntimeError("clinical cutover import attempted")
        return None

sys.meta_path.insert(0, BlockCutoverModule())
from jw_chat_agent_poc.service import app
assert {module_name!r} not in sys.modules
assert app._clinical_trials_cutover_decision(
    question={NCT_DETAIL_CASES[0]!r},
    has_documents=False,
    use_direct_agent_loop=True,
    market_scope_resolver=None,
) is None
"""
    env = dict(os.environ)
    env[service_app.CLINICAL_TRIALS_CUTOVER_ENV] = "0"

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_classification_failure_falls_back_to_legacy_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"question": FB02, "answer": "legacy", "tool_calls": []}

    class _Agent:
        def answer(self, question: str, documents, **_kwargs):
            return expected

    def fail_classification(_question: str):
        raise RuntimeError("classification unavailable")

    monkeypatch.setattr(
        service_app,
        "classify_question_without_observation",
        fail_classification,
        raising=False,
    )
    monkeypatch.setattr(service_app, "classify_question", fail_classification)
    result = service_app._answer_existing_without_pending(
        _MarketScopeStub(),
        lambda **_kwargs: _Agent(),
        "conversation",
        FB02,
        "fixture",
        None,
        SessionStore(),
        use_direct_agent_loop=True,
    )

    assert result is expected


def test_fb02_can_be_rolled_back_without_disabling_other_clinical_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(cutover.CLINICAL_TRIALS_CUTOVER_ENV, "1")
    monkeypatch.setenv(cutover.CLINICAL_FB02_CUTOVER_ENV, "0")

    assert service_app._clinical_trials_cutover_decision(
        question=FB02,
        has_documents=False,
        use_direct_agent_loop=True,
        market_scope_resolver=_MarketScopeStub(),
    ) is None
    assert service_app._clinical_trials_cutover_decision(
        question=CLINICAL_SEARCH_CASES[1],
        has_documents=False,
        use_direct_agent_loop=True,
        market_scope_resolver=_MarketScopeStub(),
    ) is not None
