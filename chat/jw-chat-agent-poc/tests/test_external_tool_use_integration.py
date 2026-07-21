from __future__ import annotations

from pathlib import Path
from typing import Any

import jw_chat_agent_poc.orchestrator.agent as agent_module
import pytest
from jw_chat_agent_poc.orchestrator.agent import ChatAgent
from jw_chat_agent_poc.orchestrator.agent import _is_external_tool_agent_candidate
from jw_chat_agent_poc.resolver import BrandResolution
from jw_chat_agent_poc.router import BQRouter, LLMFirstBQRouter


def _agent_payload(*, status: str, fallback_code: str | None) -> dict[str, Any]:
    answer = "- 리바로: 성분 = pitavastatin [로컬 시장 DB 성분 정보]"
    return {
        "question": "리바로 성분 알려줘",
        "resolution": None,
        "decomposition": [{"intent": "external_tool_agent", "status": status}],
        "router_diagnostics": {"mode": "tool_use_agent", "fallback_code": fallback_code},
        "tool_calls": [],
        "answer": answer,
        "markdown_response": {"markdown": answer, "fact_md": answer, "data_md": ""},
        "sources": ["로컬 시장 DB 성분 정보"],
    }


def test_feature_flag_routes_unclassified_external_question_to_tool_agent(monkeypatch) -> None:
    # Given: the new external-tool agent is enabled and returns verified evidence.
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "1")
    monkeypatch.setattr(
        agent_module,
        "run_external_tool_agent",
        lambda *_args, **_kwargs: _agent_payload(status="ok", fallback_code=None),
    )

    # When: a question that the legacy BQ map classifies as UNKNOWN is answered.
    result = ChatAgent(router=BQRouter()).answer("리바로 성분 알려줘")

    # Then: the tool-use result is returned instead of the legacy default/no-data path.
    assert result["router_diagnostics"]["mode"] == "tool_use_agent"
    assert "pitavastatin" in result["answer"]


@pytest.mark.parametrize(
    "routing_mode",
    ("OFF", "ENFORCE"),
)
@pytest.mark.parametrize(
    "question",
    (
        "HIRA: 상병코드 D693 환자수 알려줘",
        "상병코드 E11 2024년 환자수",
        "면역혈소판감소증 환자수 알려줘",
    ),
)
def test_direct_hira_subject_is_rejected_before_external_tool_agent(
    monkeypatch,
    question: str,
    routing_mode: str,
) -> None:
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "1")
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_FORCE_CONTRACT_CALLS", "true")
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", routing_mode)
    monkeypatch.setattr(
        agent_module,
        "run_external_tool_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsupported direct HIRA subjects must stop before provider execution")
        ),
    )

    result = ChatAgent(router=BQRouter()).answer(question)

    assert result["tool_calls"] == []
    assert result["sources"] == ["unsupported_hira_interface"]
    assert "상병코드 또는 질환명 직접 조회" in result["answer"]


def test_field_not_exposed_nedrug_question_stops_before_legacy_provider(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "1")
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_FORCE_CONTRACT_CALLS", "true")
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "OFF")
    monkeypatch.setattr(
        agent_module,
        "run_external_tool_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("FIELD_NOT_EXPOSED must stop before provider execution")
        ),
    )

    result = ChatAgent(router=BQRouter()).answer("NeDrug: 리바로 e약 효능 알려줘")

    assert result["tool_calls"] == []
    assert result["sources"] == ["field_not_exposed"]
    assert "현재 연결에서 제공되지 않습니다" in result["answer"]


def test_external_tool_agent_receives_and_returns_request_timing(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_tool_agent(*_args, **kwargs):
        captured["timing"] = kwargs.get("timing")
        payload = _agent_payload(status="ok", fallback_code=None)
        payload["timing"] = kwargs["timing"]
        return payload

    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "1")
    monkeypatch.setattr(agent_module, "run_external_tool_agent", fake_tool_agent)

    result = ChatAgent(router=BQRouter()).answer("리바로 성분 알려줘")

    assert isinstance(captured["timing"], dict)
    assert result["timing"] is captured["timing"]


def test_tool_agent_is_enabled_by_default_for_external_question(monkeypatch) -> None:
    # Given: no runtime override is present and the tool agent returns verified evidence.
    monkeypatch.delenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", raising=False)
    monkeypatch.setattr(
        agent_module,
        "run_external_tool_agent",
        lambda *_args, **_kwargs: _agent_payload(status="ok", fallback_code=None),
    )

    # When: an external-evidence question reaches the orchestrator.
    result = ChatAgent(router=BQRouter()).answer("리바로 성분 알려줘")

    # Then: the structural tool-use path is the default.
    assert result["router_diagnostics"]["mode"] == "tool_use_agent"


def test_feature_flag_off_preserves_legacy_path(monkeypatch) -> None:
    # Given: the feature flag is disabled.
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "0")
    monkeypatch.setattr(
        agent_module,
        "run_external_tool_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("tool agent must remain off")),
    )

    # When: the same question uses the legacy path.
    result = ChatAgent(router=BQRouter()).answer("리바로 성분 알려줘")

    # Then: no tool-use mode is reported.
    assert result["router_diagnostics"].get("mode") != "tool_use_agent"


def test_feature_flag_off_preserves_legacy_guideline_source_trap(monkeypatch) -> None:
    # Given: the new tool-use path is disabled.
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "0")

    # When: the legacy source-trap vocabulary sees a generic guideline request.
    result = ChatAgent(router=BQRouter()).answer("최신 고지혈증 가이드라인")

    # Then: the feature branch has not changed the legacy answer contract.
    assert result["answer"].startswith(
        "NCCN/가이드라인 데이터는 현재 운영 데이터에 미보유입니다."
    )
    assert result["router_diagnostics"].get("mode") != "tool_use_agent"


def test_infrastructure_failure_falls_back_with_reason_code(monkeypatch) -> None:
    # Given: the feature is enabled but the planner times out.
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "1")
    monkeypatch.setattr(
        agent_module,
        "run_external_tool_agent",
        lambda *_args, **_kwargs: _agent_payload(status="fallback", fallback_code="TOOL_TIMEOUT"),
    )

    # When: the external candidate question is answered.
    result = ChatAgent(router=BQRouter()).answer("리바로 성분 알려줘")

    # Then: legacy behavior remains available and the fallback is not silent.
    assert result["router_diagnostics"]["mode"] != "tool_use_agent"
    assert result["router_diagnostics"]["external_tool_agent_fallback_code"] == "TOOL_TIMEOUT"


def test_empty_evidence_failure_is_terminal_before_legacy_generation(monkeypatch) -> None:
    # Given: the new tool path found no verifiable evidence.
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "1")
    monkeypatch.setattr(
        agent_module,
        "run_external_tool_agent",
        lambda *_args, **_kwargs: _agent_payload(
            status="fallback",
            fallback_code="VERIFICATION_FAIL",
        ),
    )

    # When: the external candidate is answered.
    result = ChatAgent(router=BQRouter()).answer("리바로 성분 알려줘")

    # Then: the explicit failure remains in tool-use mode so GenosClient bypasses final LLM generation.
    assert result["router_diagnostics"]["mode"] == "tool_use_agent"
    assert result["router_diagnostics"]["fallback_code"] == "VERIFICATION_FAIL"


def test_known_no_data_boundary_does_not_enter_external_tool_agent(monkeypatch) -> None:
    # Given: the tool agent is enabled, but the router has a deliberate no-data contract.
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "1")
    monkeypatch.setattr(
        agent_module,
        "run_external_tool_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("known no-data routes must stay in the legacy contract")
        ),
    )

    # When: a forecast request reaches the orchestrator.
    result = ChatAgent(router=BQRouter()).answer("리바로 매출 예측")

    # Then: the explicit no-data boundary remains authoritative.
    assert result["router_diagnostics"].get("mode") != "tool_use_agent"


def test_market_metric_route_does_not_enter_external_tool_agent(monkeypatch) -> None:
    # Given: the feature is enabled alongside the existing market path.
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "1")
    monkeypatch.setattr(
        agent_module,
        "run_external_tool_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("market metrics must stay outside the external tool pack")
        ),
    )

    # When: a normal market metric is requested.
    result = ChatAgent(router=BQRouter()).answer("리바로 매출")

    # Then: the existing market contract remains the only execution path.
    assert result["router_diagnostics"].get("mode") != "tool_use_agent"


def test_attached_document_does_not_enter_external_tool_agent() -> None:
    # Given: an explicit document request carries an attached document.
    routes = BQRouter().route("이 문서를 요약해줘", has_documents=True)

    # When: the external tool-pack boundary is evaluated.
    is_candidate = _is_external_tool_agent_candidate(
        routes,
        [Path("report.pdf")],
        question="이 문서를 요약해줘",
    )

    # Then: file evidence remains outside the external MCP strangler path.
    assert is_candidate is False


@pytest.mark.parametrize(
    "question",
    (
        "리바로 top3",
        "영화 시장 top5",
        "화장품 브랜드 top5",
        "자동차 브랜드 상위 5개",
    ),
)
def test_top_n_intent_does_not_enter_external_web_agent(question: str) -> None:
    routes = BQRouter().route(question)

    assert _is_external_tool_agent_candidate(routes, [], question=question) is False


@pytest.mark.parametrize(
    "question",
    (
        "최신 임상 연구에서 상위 5% 환자군 결과를 검색해줘",
        "최신 임상 연구에서 상위 2.5% 환자군 결과를 검색해줘",
        "Top 2.5 mg dose clinical evidence",
        "Top 10 mg 용량의 최신 임상 근거를 찾아줘",
        "Top 10-mg dose clinical evidence",
        "Top 10 mg/day dosage clinical evidence",
        "Top 10 ng 용량의 최신 임상 근거를 찾아줘",
        "Top 10 µg dosage clinical evidence",
        "Top 10 IU 투여량의 최신 임상 근거를 찾아줘",
    ),
)
def test_non_ranking_numeric_research_can_enter_external_tool_agent(question: str) -> None:
    routes = BQRouter().route(question)

    assert _is_external_tool_agent_candidate(routes, [], question=question) is True


@pytest.mark.parametrize(
    "question",
    (
        "top 5 ML models",
        "top 10 MG cars",
        "latest top 5 ML models for dosage prediction",
        "latest top 5 ML dose prediction models",
    ),
)
def test_uppercase_ranking_nouns_remain_top_n_and_fail_closed(question: str) -> None:
    routes = BQRouter().route(question)

    assert _is_external_tool_agent_candidate(routes, [], question=question) is False


@pytest.mark.parametrize("question", ("리바로 top3", "화장품 브랜드 top5"))
def test_top_n_request_never_calls_external_web_agent(monkeypatch, question: str) -> None:
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "1")
    monkeypatch.setattr(
        agent_module,
        "run_external_tool_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Top-N intent must stay on the grounded market boundary")
        ),
    )

    result = ChatAgent(router=BQRouter()).answer(question)

    assert result["answer"].strip()
    assert result["router_diagnostics"].get("mode") != "tool_use_agent"


@pytest.mark.parametrize("misclassified_tool", ("external_api", "document"))
def test_llm_first_top_n_misclassification_never_calls_external_web_agent(
    monkeypatch,
    misclassified_tool: str,
) -> None:
    class ExternalMisclassification:
        def decompose(self, _question: str, _has_documents: bool) -> str:
            return (
                f'{{"bq_ids":["Q1"],"tools":["{misclassified_tool}"],"brands":[],'
                '"no_data_flag":false,"confidence":0.99,"reason":"misclassified research"}'
            )

    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "1")
    monkeypatch.setattr(
        agent_module,
        "run_external_tool_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("LLM decomposition must not override the Top-N scope lock")
        ),
    )

    result = ChatAgent(router=LLMFirstBQRouter(decomposer=ExternalMisclassification())).answer(
        "화장품 브랜드 top5"
    )

    assert result["answer"].strip()
    assert result["router_diagnostics"].get("mode") != "tool_use_agent"


def test_unattached_guideline_research_can_enter_external_tool_agent(monkeypatch) -> None:
    # Given: the router labels guideline research as document-oriented, but no file is attached.
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "1")
    monkeypatch.setattr(
        agent_module,
        "run_external_tool_agent",
        lambda *_args, **_kwargs: _agent_payload(status="ok", fallback_code=None),
    )

    # When: the user asks for current external guidance.
    result = ChatAgent(router=BQRouter()).answer("최신 고지혈증 가이드라인")

    # Then: the external tool pack remains available for the Tavily path.
    assert result["router_diagnostics"]["mode"] == "tool_use_agent"


def test_guideline_tool_pack_selection_precedes_llm_bq_decomposition(monkeypatch) -> None:
    # Given: the external tool pack is enabled and live BQ decomposition would be unnecessary.
    class UnexpectedDecomposer:
        def decompose(self, _question: str, _has_documents: bool) -> str:
            raise AssertionError("tool-pack selection must precede LLM BQ decomposition")

    class UnbrandedResolver:
        def has_fixture_alias(self, _question: str) -> bool:
            return False

        def resolve(self, _question: str, allow_default: bool = False) -> BrandResolution:
            del allow_default
            raise AssertionError("unbranded web research must not load the brand catalog")

    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "1")
    monkeypatch.setattr(
        agent_module,
        "run_external_tool_agent",
        lambda *_args, **_kwargs: _agent_payload(status="ok", fallback_code=None),
    )

    # When: generic guideline research is answered by the production LLM-first router.
    result = ChatAgent(
        router=LLMFirstBQRouter(decomposer=UnexpectedDecomposer()),
        resolver=UnbrandedResolver(),
    ).answer("최신 고지혈증 가이드라인")

    # Then: the existing deterministic pack selector enters ToolSpec selection directly.
    assert result["router_diagnostics"]["mode"] == "tool_use_agent"


def test_structural_external_contract_precedes_direct_bq_loop(monkeypatch) -> None:
    # Given: clinical and patent questions already have declarative answer contracts.
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "1")
    monkeypatch.setattr(
        agent_module,
        "run_external_tool_agent",
        lambda *_args, **_kwargs: _agent_payload(status="ok", fallback_code=None),
    )
    monkeypatch.setattr(
        agent_module,
        "build_tool_use_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("structural external contracts must enter ToolSpec before the direct BQ loop")
        ),
    )

    for question in ("리바로 임상시험", "리바로 특허 만료일"):
        result = ChatAgent(router=BQRouter()).answer(question)

        assert result["router_diagnostics"]["mode"] == "tool_use_agent"


def test_unavailable_source_contract_precedes_external_tool_agent(monkeypatch) -> None:
    # Given: the user explicitly asks for a source the product does not own.
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "1")
    monkeypatch.setattr(
        agent_module,
        "run_external_tool_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("source-trap requests must fail closed before tool selection")
        ),
    )

    # When: the unavailable source is requested for a supported brand.
    result = ChatAgent(router=BQRouter()).answer("리바로 Cortellis 임상 알려줘")

    # Then: the existing unavailable-source response remains authoritative.
    assert "Cortellis" in result["answer"]
    assert result["router_diagnostics"].get("mode") != "tool_use_agent"


def test_ambiguous_market_contract_precedes_external_tool_agent(monkeypatch) -> None:
    # Given: the resolver requires the user to choose one of two markets.
    class AmbiguousResolver:
        def resolve(self, _question: str, allow_default: bool = False) -> BrandResolution:
            del allow_default
            return BrandResolution(
                canonical_brand="리바로",
                audit_code="",
                molecule_en=("pitavastatin",),
                atc=("C10A1",),
                edi_code=None,
                item_seq=None,
                is_combo=False,
                market_ids=("m1", "m2"),
                market_names=("시장 1", "시장 2"),
                support_source="catalog_membership",
            )

    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "1")
    monkeypatch.setattr(
        agent_module,
        "run_external_tool_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("market clarification must precede tool selection")
        ),
    )

    # When: an external-evidence question is asked for the ambiguous brand.
    result = ChatAgent(router=BQRouter(), resolver=AmbiguousResolver()).answer("리바로 임상 알려줘")

    # Then: the user receives the required market clarification instead.
    assert result["router_diagnostics"]["scope"] == "market_ambiguity"
    assert result["tool_calls"] == []
