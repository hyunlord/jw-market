from __future__ import annotations

from types import SimpleNamespace

import pytest

from jw_chat_agent_poc.agent_loop.models import AgentObservation
from jw_chat_agent_poc.agent_loop.external_tools import safety_call
from jw_chat_agent_poc.agent_loop import loop as loop_module
from jw_chat_agent_poc.agent_loop.tools import ToolExecution
from jw_chat_agent_poc.common import timing as timing_module
from jw_chat_agent_poc.genos_config import (
    resolve_deep_genos_base_url,
    resolve_deep_genos_token,
    resolve_final_genos_base_url,
)
from jw_chat_agent_poc.orchestrator.deep_research import (
    DeepResearchToolPlanner,
    parse_deep_research_request,
)
from jw_chat_agent_poc.orchestrator.claim_policy import claim_policy_report
from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service import genos_client as genos_module
from jw_chat_agent_poc.service.app import SessionStore
from jw_chat_agent_poc.service.genos_client import GenosClient
from jw_chat_agent_poc.tools.external import ExternalCall


@pytest.mark.parametrize(
    ("raw", "enabled", "question"),
    (
        ("/deep 리바로 경쟁구도 분석", True, "리바로 경쟁구도 분석"),
        ("/deep\n리바로 경쟁구도 분석", True, "리바로 경쟁구도 분석"),
        ("/deep", True, ""),
        ("/deepdive 리바로", False, "/deepdive 리바로"),
        ("리바로 /deep 분석", False, "리바로 /deep 분석"),
        ("리바로 딥리서치 해줘", False, "리바로 딥리서치 해줘"),
    ),
)
def test_deep_trigger_is_exact_and_leading(raw: str, enabled: bool, question: str) -> None:
    parsed = parse_deep_research_request(raw)

    assert parsed.enabled is enabled
    assert parsed.question == question
    assert parsed.original_question == raw


def test_deep_serving_is_isolated_from_general_final(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENOS_BASE_URL", "https://example.test/api/gateway/rep/serving/517")
    monkeypatch.setenv("GENOS_FINAL_SERVING_ID", "514")
    monkeypatch.setenv("GENOS_DEEP_SERVING_ID", "202")
    monkeypatch.setenv("GENOS_FINAL_BEARER_TOKEN", "final-token")
    monkeypatch.setenv("GENOS_DEEP_BEARER_TOKEN", "deep-token")

    assert resolve_final_genos_base_url().endswith("/serving/514")
    assert resolve_deep_genos_base_url().endswith("/serving/202")
    assert resolve_deep_genos_token() == "deep-token"

    general = GenosClient()
    deep = GenosClient.for_deep_research()

    assert general.base_url.endswith("/serving/514")
    assert general.research_mode == "standard"
    assert deep.base_url.endswith("/serving/202")
    assert deep.token == "deep-token"
    assert deep.research_mode == "deep"
    assert deep.model == "gemini-3.1-pro-preview"
    assert general.model is None


def test_deep_serving_default_does_not_inherit_common_serving_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GENOS_BASE_URL", "https://example.test/api/gateway/rep/serving/517")
    monkeypatch.setenv("GENOS_SERVING_ID", "517")
    monkeypatch.delenv("GENOS_DEEP_SERVING_ID", raising=False)

    assert resolve_deep_genos_base_url().endswith("/serving/202")


def test_deep_request_sends_preview_model_to_serving_202(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def iter_lines(self, *, decode_unicode: bool):
            assert decode_unicode is True
            return iter((
                'data: {"choices":[{"delta":{"content":"완료"}}]}',
                "data: [DONE]",
            ))

        def close(self) -> None:
            return None

    def post(url: str, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(genos_module.requests, "post", post)
    client = GenosClient(
        base_url="https://example.test/api/gateway/rep/serving/202",
        token="deep-token",
        research_mode="deep",
        model="gemini-3.1-pro-preview",
    )

    assert "".join(client._stream_chat([{"role": "user", "content": "질문"}])) == "완료"
    assert captured["url"] == "https://example.test/api/gateway/rep/serving/202/chat/completions"
    assert captured["headers"] == {"Authorization": "Bearer deep-token"}
    assert captured["json"]["model"] == "gemini-3.1-pro-preview"


def test_deep_planner_requests_broad_independent_evidence() -> None:
    planner = DeepResearchToolPlanner()
    schemas = tuple(
        {
            "type": "function",
            "function": {"name": name, "parameters": {"type": "object"}},
        }
        for name in (
            "get_metric",
            "get_market_scope",
            "get_brand_series",
            "get_top_brands",
            "search_news",
            "get_disease_stats",
            "search_clinical",
            "search_drug_info",
            "search_safety",
            "search_patent",
            "csd_activity_trend",
            "web_search",
        )
    )

    decision = planner.decide(
        "리바로 경쟁구도 분석",
        (),
        schemas,
        ("리바로",),
        ("2026-05",),
    )

    names = [call.name for call in decision.tool_calls]
    assert {
        "get_metric",
        "get_market_scope",
        "get_brand_series",
        "get_top_brands",
        "search_news",
        "get_disease_stats",
        "search_clinical",
        "search_drug_info",
        "search_safety",
        "search_patent",
        "csd_activity_trend",
    }.issubset(names)
    news_call = next(call for call in decision.tool_calls if call.name == "search_news")
    assert news_call.arguments == {"brand": "리바로", "query": ""}
    web_queries = [call.arguments["query"] for call in decision.tool_calls if call.name == "web_search"]
    assert len(web_queries) >= 3
    assert len(set(web_queries)) == len(web_queries)

    completed = planner.decide(
        "리바로 경쟁구도 분석",
        (
            AgentObservation(
                step=1,
                tool_name="search_clinical",
                arguments={"brand": "리바로"},
                status="ok",
                preview="1건",
            ),
        ),
        schemas,
        ("리바로",),
        ("2026-05",),
    )
    assert completed.tool_calls == ()


def test_deep_progress_labels_are_distinct_user_language() -> None:
    assert timing_module._public_stage_name("deep_research_prepare") == "딥리서치 질문 분석"
    assert timing_module._public_stage_name("deep_research_plan") == "딥리서치 조사 설계"
    assert timing_module._public_stage_name("deep_research_tool_batch") == "딥리서치 자료 수집"
    assert timing_module._public_stage_name("deep_research_synthesis") == "딥리서치 종합 분석"
    assert timing_module._public_stage_name("llm_plan") == "분석 계획"
    assert timing_module._public_stage_name("tool:search_clinical") == "임상시험 통합 조회"
    assert timing_module._public_stage_name("tool:search_drug_info") == "식약처 허가 정보 확인"
    assert timing_module._public_stage_name("tool:get_disease_stats") == "건강보험 환자 정보 확인"


def test_deep_progress_counts_nested_tool_evidence() -> None:
    execution = ToolExecution(
        status="ok",
        preview="clinical evidence",
        arguments={"brand": "리바로"},
        call={
            "status": "ok",
            "render_data": {
                "calls": [
                    {"render_data": {"items": [{"id": "NCT1"}, {"id": "NCT2"}]}},
                    {"render_data": {"items": [{"id": "MFDS1"}]}},
                ]
            },
        },
    )

    assert loop_module._deep_tool_progress_summary(execution) == "3건 확인"


def test_deep_request_routes_with_stripped_question_and_preserves_original(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class ScopeResolver:
        def has_explicit_anchor(self, question: str) -> bool:
            captured["scope_question"] = question
            return True

    def deep_answer(question: str, external_mode: str) -> dict[str, object]:
        captured["deep"] = (question, external_mode)
        return {
            "answer": "deep-answer",
            "sources": ["cache"],
            "tool_calls": [],
            "research_mode": "deep",
            "router_diagnostics": {"mode": "deep_research"},
        }

    monkeypatch.setattr(service_app, "_answer_deep_research", deep_answer)

    item = service_app._answer_question(
        SessionStore(),
        ScopeResolver(),
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("general agent must not run")),
        "/deep 리바로 경쟁구도 분석",
        "live",
        None,
        use_direct_agent_loop=True,
    )

    assert captured["scope_question"] == "리바로 경쟁구도 분석"
    assert captured["deep"] == ("리바로 경쟁구도 분석", "live")
    assert item["question"] == "/deep 리바로 경쟁구도 분석"
    assert item["result"]["effective_question"] == "리바로 경쟁구도 분석"
    assert item["result"]["research_mode"] == "deep"


def test_deep_fixture_execution_selects_all_evidence_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAT_METRICS_MODE", "fixture")
    monkeypatch.setenv("CHAT_QUERY_LAYER_ENABLED", "0")
    events: list[dict[str, object]] = []

    with timing_module.stage_event_sink(events.append):
        result = service_app._answer_deep_research("리바로 경쟁구도 분석", "fixture")

    selected = set(result["agent_loop_metrics"]["selected_tools"])
    assert {
        "get_metric",
        "get_market_scope",
        "search_news",
        "get_disease_stats",
        "search_clinical",
        "search_drug_info",
        "search_safety",
        "search_patent",
        "csd_activity_trend",
        "web_search",
    }.issubset(selected)
    assert result["research_mode"] == "deep"
    assert result["router_diagnostics"]["model"] == "gemini-3.1-pro-preview"
    assert any(event.get("raw_name") == "deep_research_tool_batch" for event in events)
    assert any(
        event.get("raw_name") == "tool:search_clinical" and event.get("status") == "done"
        for event in events
    )
    assert not any("clinicaltrials_v2_search" in str(event) for event in events)


def test_deep_client_bypasses_general_deterministic_shortcuts(monkeypatch: pytest.MonkeyPatch) -> None:
    def shortcut_bomb(*_args, **_kwargs):
        raise AssertionError("general deterministic shortcut must not run in deep mode")

    monkeypatch.setattr(genos_module, "deterministic_single_period_sales_answer", shortcut_bomb)
    monkeypatch.setattr(genos_module, "deterministic_top_n_share_answer", shortcut_bomb)
    monkeypatch.setattr(
        GenosClient,
        "_markdown_answer",
        lambda self, question, markdown_response, timing=None, tool_calls=None, file_context="": "딥리서치 합성",
    )
    client = GenosClient(token="deep-token", research_mode="deep")

    answer = "".join(
        client.stream_answer(
            "리바로 경쟁구도 분석",
            {
                "markdown_response": {
                    "fact_md": "- 리바로: 확인된 근거",
                    "data_md": "- 리바로: 확인된 근거",
                    "allowed_numbers": [],
                },
                "tool_calls": [],
                "router_diagnostics": {"mode": "deep_research"},
            },
        )
    )

    assert answer == "딥리서치 합성"


def test_deep_prompt_requires_grounded_multi_source_synthesis() -> None:
    messages = GenosClient._deep_markdown_messages(
        "리바로 경쟁구도 분석",
        {
            "fact_md": "- 리바로: 확인된 근거",
            "data_md": "- 리바로: 확인된 근거",
        },
    )

    assert "딥리서치 모드" in messages[0]["content"]
    assert "수치, URL, 기사, 인과, 전망을 만들지 않는다" in messages[0]["content"]
    assert "시장·경쟁 구도" in messages[0]["content"]
    assert "임상·허가·안전성·환자 맥락" in messages[0]["content"]
    assert "출처별 근거" in messages[1]["content"]


def test_compute_final_answer_uses_deep_client_and_stripped_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class DeepClient:
        token_usage_calls: list[dict[str, object]] = []

        @classmethod
        def for_deep_research(cls):
            captured["client"] = "deep"
            return cls()

        def stream_answer(self, question: str, result: dict[str, object]):
            captured["question"] = question
            captured["mode"] = result.get("research_mode")
            yield "확인된 근거를 종합했습니다."

    monkeypatch.setattr(service_app, "GenosClient", DeepClient)
    monkeypatch.setattr(
        service_app,
        "_deterministic_simple_market_answer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("deep mode must not use the general fast path")
        ),
    )

    final = service_app.compute_final_answer(
        "/deep 리바로 경쟁구도 분석",
        {
            "research_mode": "deep",
            "effective_question": "리바로 경쟁구도 분석",
            "context_scope": "MARKET",
            "answer": "확인된 근거",
            "sources": ["cache"],
            "tool_calls": [],
            "router_diagnostics": {"mode": "deep_research"},
            "markdown_response": {
                "fact_md": "- 리바로: 확인된 근거",
                "data_md": "- 리바로: 확인된 근거",
                "allowed_numbers": [],
            },
        },
    )

    assert captured == {
        "client": "deep",
        "question": "리바로 경쟁구도 분석",
        "mode": "deep",
    }
    assert final.trace["question"] == "/deep 리바로 경쟁구도 분석"
    assert any(
        item["name"] == "딥리서치 종합 분석"
        for item in final.timing["stages"]
    )


def test_compute_final_answer_keeps_deep_synthesis_out_of_general_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rich_answer = """## 핵심 요약

리바로의 확인된 시장 근거와 외부 근거를 함께 보면 경쟁 상황을 여러 관점에서 살펴볼 수 있습니다.

## 시장·경쟁 구도

확인된 시장 수치만 사용했습니다.

## 임상·허가·안전성·환자 맥락

각 외부 출처에서 확인된 항목을 구분해 정리했습니다.

## 종합 판단과 한계

근거가 없는 인과나 전망은 단정하지 않았습니다.
"""
    contract_calls: list[str] = []

    class DeepClient:
        token_usage_calls: list[dict[str, object]] = []

        @classmethod
        def for_deep_research(cls):
            return cls()

        def stream_answer(self, _question: str, _result: dict[str, object]):
            yield rich_answer

    def collapse_answer(*_args, **_kwargs) -> str:
        contract_calls.append("general")
        return "일반 시장 계약이 딥리서치 응답을 덮었습니다."

    monkeypatch.setattr(service_app, "GenosClient", DeepClient)
    monkeypatch.setattr(service_app, "enforce_answer_contract", collapse_answer)
    monkeypatch.setattr(service_app, "enforce_market_answer_contract", collapse_answer)

    final = service_app.compute_final_answer(
        "/deep 리바로 경쟁구도 분석",
        {
            "research_mode": "deep",
            "effective_question": "리바로 경쟁구도 분석",
            "context_scope": "MARKET",
            "answer": "확인된 근거",
            "sources": ["cache"],
            "tool_calls": [],
            "router_diagnostics": {"mode": "deep_research"},
            "markdown_response": {
                "fact_md": "- 리바로: 확인된 근거",
                "data_md": "- 리바로: 확인된 근거",
                "allowed_numbers": [],
            },
        },
    )

    assert contract_calls == []
    assert "## 핵심 요약" in final.text
    assert "## 시장·경쟁 구도" in final.text
    assert "## 임상·허가·안전성·환자 맥락" in final.text
    assert "## 종합 판단과 한계" in final.text


def test_compute_final_answer_applies_claim_policy_after_deep_text_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DeepClient:
        token_usage_calls: list[dict[str, object]] = []

        @classmethod
        def for_deep_research(cls):
            return cls()

        def stream_answer(self, _question: str, _result: dict[str, object]):
            yield "## 핵심 요약\n\n확인된 사실만 정리했습니다."

    def append_forbidden_claim(_question: str, answer: str, _fact_md: str) -> str:
        return f"{answer}\n\n뉴스에서 시장 성과가 입증됐습니다."

    fact_md = "인사이트 근거 fact - 뉴스/이슈\n- search_news: 관련 기사"
    monkeypatch.setattr(service_app, "GenosClient", DeepClient)
    monkeypatch.setattr(service_app, "ensure_natural_fact_lead", append_forbidden_claim)
    monkeypatch.setattr(
        service_app,
        "enforce_answer_contract",
        lambda _question, answer, _markdown, _contract=None: answer,
    )
    monkeypatch.setattr(
        service_app,
        "enforce_market_answer_contract",
        lambda _question, answer, _calls: answer,
    )

    final = service_app.compute_final_answer(
        "/deep 리바로 경쟁구도 분석",
        {
            "research_mode": "deep",
            "effective_question": "리바로 경쟁구도 분석",
            "context_scope": "MARKET",
            "answer": "확인된 근거",
            "sources": ["news"],
            "tool_calls": [],
            "router_diagnostics": {"mode": "deep_research"},
            "markdown_response": {
                "fact_md": fact_md,
                "data_md": fact_md,
                "allowed_numbers": [],
            },
        },
    )

    assert "뉴스에서 시장 성과가 입증됐습니다" not in final.text
    assert claim_policy_report(final.text, fact_md)["forbidden_claims_remaining"] == ()


def test_compute_final_answer_drops_unverified_deep_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GenosClient(
        base_url="https://example.test/api/gateway/rep/serving/202",
        token="deep-token",
        research_mode="deep",
        model="gemini-3.1-pro-preview",
    )
    monkeypatch.setattr(
        GenosClient,
        "for_deep_research",
        classmethod(lambda cls: client),
    )
    monkeypatch.setattr(
        GenosClient,
        "_chat_text",
        lambda self, messages: "경쟁사의 공격적 마케팅 때문에 2028년 매출이 999억원으로 반등합니다.",
    )

    final = service_app.compute_final_answer(
        "/deep 리바로 경쟁구도 분석",
        {
            "research_mode": "deep",
            "effective_question": "리바로 경쟁구도 분석",
            "context_scope": "MARKET",
            "answer": "확인된 근거",
            "sources": ["cache"],
            "tool_calls": [],
            "router_diagnostics": {"mode": "deep_research"},
            "markdown_response": {
                "fact_md": "- 리바로 2026-05 매출 = 80.39억원",
                "data_md": "- 리바로 2026-05 매출 = 80.39억원",
                "allowed_numbers": ["2026-05", "80.39"],
            },
        },
    )

    assert "999" not in final.text
    assert "2028" not in final.text
    assert "공격적 마케팅 때문에" not in final.text


def test_safety_tool_uses_grounded_molecule() -> None:
    class SafetyExternal:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def openfda_label_search(self, molecule: str) -> ExternalCall:
            self.queries.append(molecule)
            return ExternalCall(
                tool="openfda_label_search",
                source="openfda_mcp",
                status="ok",
                summary_text=f"{molecule} FDA 라벨",
                render_data={"items": [{"generic_name": molecule}]},
            )

    external = SafetyExternal()
    resolution = SimpleNamespace(
        canonical_brand="리바로",
        molecule_en=("PITAVASTATIN",),
        is_combo=False,
    )

    call = safety_call(resolution, external)

    assert external.queries == ["PITAVASTATIN"]
    assert call["status"] == "ok"
    assert call["render_data"]["facade_tool"] == "search_safety"
