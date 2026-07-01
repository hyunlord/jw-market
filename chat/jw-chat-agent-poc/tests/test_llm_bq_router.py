from __future__ import annotations

from dataclasses import dataclass

from jw_chat_agent_poc import ChatAgent
from jw_chat_agent_poc.router import LLMFirstBQRouter


@dataclass(frozen=True, slots=True)
class FakeDecomposer:
    output: str

    def decompose(self, question: str, has_documents: bool) -> str:
        return self.output


@dataclass(frozen=True, slots=True)
class FailingDecomposer:
    def decompose(self, question: str, has_documents: bool) -> str:
        raise RuntimeError("offline")  # noqa: GENERIC_ERR_OK


def test_llm_router_accepts_structured_multi_bq_output() -> None:
    router = LLMFirstBQRouter(
        decomposer=FakeDecomposer(
            '{"bq_ids":["Q2","Q2.5"],"tools":["metrics","external_api"],'
            '"brands":["리바로"],"no_data_flag":false,"confidence":0.91,"reason":"경쟁과 임상"}'
        )
    )

    routes = router.route("리바로 경쟁이랑 임상")

    assert {route.bq for route in routes} == {"Q2", "Q2.5"}
    assert all(route.sources == ("metrics", "external_api") for route in routes)
    assert router.last_diagnostics.mode == "llm"
    assert router.last_diagnostics.fallback_used is False


def test_llm_router_preserves_portfolio_scope_from_structured_output() -> None:
    router = LLMFirstBQRouter(
        decomposer=FakeDecomposer(
            '{"bq_ids":["Q2"],"tools":["metrics"],"brands":[],'
            '"scope":"portfolio","no_data_flag":false,"confidence":0.91,"reason":"자사 브랜드 집합 하락 분석"}'
        )
    )

    routes = router.route("JW 주요 브랜드 중 하락한 거 원인 분석")

    assert routes[0].scope == "portfolio"
    assert routes[0].sources == ("metrics",)
    assert router.last_diagnostics.mode == "llm"


def test_keyword_router_classifies_short_portfolio_decline_variants() -> None:
    router = LLMFirstBQRouter(decomposer=FailingDecomposer())

    questions = [
        "JW 주요 브랜드 중 하락한 거 원인 분석",
        "JW 주요 브랜드 중 떨어진 브랜드 원인 분석",
        "부진한 자사 제품 알려줘",
        "우리 제품 중 밀리는 거 원인 분석",
        "JW 포트폴리오에서 하락한 브랜드 원인 분석",
    ]

    for question in questions:
        routes = router.route(question)
        assert routes[0].scope == "portfolio"
        assert routes[0].sources == ("metrics",)


def test_llm_router_falls_back_on_invalid_json() -> None:
    router = LLMFirstBQRouter(decomposer=FakeDecomposer("not json"))

    routes = router.route("리바로 매출 알려줘")

    assert routes[0].bq == "Q1"
    assert routes[0].sources == ("metrics",)
    assert router.last_diagnostics.fallback_used is True
    assert router.last_diagnostics.reason.startswith("llm_failed")


def test_llm_router_falls_back_on_invalid_tool() -> None:
    router = LLMFirstBQRouter(
        decomposer=FakeDecomposer(
            '{"bq_ids":["Q1"],"tools":["made_up_tool"],"brands":["리바로"],'
            '"no_data_flag":false,"confidence":0.99}'
        )
    )

    routes = router.route("리바로 시장규모")

    assert routes[0].bq == "Q1"
    assert routes[0].sources == ("metrics",)
    assert router.last_diagnostics.fallback_used is True
    assert router.last_diagnostics.reason == "empty_or_no_action"


def test_keyword_router_routes_patient_count_to_hira_external_api_before_metrics() -> None:
    router = LLMFirstBQRouter(decomposer=FailingDecomposer())

    routes = router.route("리바로 관련 질병 환자수")

    assert routes[0].bq == "Q1"
    assert routes[0].sources == ("external_api",)
    assert router.last_diagnostics.fallback_used is True


def test_keyword_router_routes_news_questions_to_deep_analysis_events() -> None:
    router = LLMFirstBQRouter(decomposer=FailingDecomposer())

    routes = router.route("리바로젯 최근 이슈 알려줘")

    assert routes[0].bq == "Q1"
    assert routes[0].sources == ("deep_analysis_events",)
    assert "뉴스" in routes[0].question
    assert router.last_diagnostics.fallback_used is True


def test_keyword_router_extracts_news_source_filter() -> None:
    router = LLMFirstBQRouter(decomposer=FailingDecomposer())

    routes = router.route("리바로 뉴스 약업신문 것만")

    assert routes[0].sources == ("deep_analysis_events",)
    assert routes[0].filters == (("source", "약업신문"),)


def test_llm_router_accepts_news_filters_from_structured_output() -> None:
    router = LLMFirstBQRouter(
        decomposer=FakeDecomposer(
            '{"bq_ids":["Q1"],"tools":["deep_analysis_events"],"brands":["리바로"],'
            '"filters":{"source":"약업신문","recent_days":30},'
            '"no_data_flag":false,"confidence":0.91,"reason":"뉴스 출처와 기간"}'
        )
    )

    routes = router.route("리바로 약업신문 최근 한 달 뉴스")

    assert routes[0].sources == ("deep_analysis_events",)
    assert routes[0].filters == (("recent_days", 30), ("source", "약업신문"))
    assert routes[0].brands == ("리바로",)
    assert router.last_diagnostics.mode == "llm"


def test_llm_router_preserves_news_brand_metadata() -> None:
    router = LLMFirstBQRouter(
        decomposer=FakeDecomposer(
            '{"bq_ids":["Q1"],"tools":["deep_analysis_events"],"brands":["리바로","아토젯"],'
            '"filters":{"title_contains":"약가"},'
            '"no_data_flag":false,"confidence":0.91,"reason":"복수 브랜드 관련 뉴스"}'
        )
    )

    routes = router.route("리바로 아토젯 둘 다 관련 뉴스 중 제목에 약가")

    assert routes[0].sources == ("deep_analysis_events",)
    assert routes[0].brands == ("리바로", "아토젯")
    assert routes[0].filters == (("title_contains", "약가"),)


def test_llm_router_accepts_llm_only_news_text_filter() -> None:
    router = LLMFirstBQRouter(
        decomposer=FakeDecomposer(
            '{"bq_ids":["Q1"],"tools":["deep_analysis_events"],"brands":["리바로"],'
            '"filters":{"title_contains":"약가"},'
            '"no_data_flag":false,"confidence":0.91,"reason":"뉴스 제목 조건"}'
        )
    )

    routes = router.route("리바로 제목 조건 뉴스")

    assert routes[0].sources == ("deep_analysis_events",)
    assert routes[0].filters == (("title_contains", "약가"),)


def test_keyword_router_extracts_metrics_filters() -> None:
    router = LLMFirstBQRouter(decomposer=FailingDecomposer())

    routes = router.route("리바로 작년 상급종병 M/S IQVIA 기준")

    assert routes[0].sources == ("metrics",)
    assert routes[0].filters == (
        ("channel", "상급종병"),
        ("period", "previous_year"),
        ("source", "IQVIA"),
    )


def test_llm_router_accepts_metrics_filters_from_structured_output() -> None:
    router = LLMFirstBQRouter(
        decomposer=FakeDecomposer(
            '{"bq_ids":["Q1"],"tools":["metrics"],"brands":["리바로"],'
            '"filters":{"source":"IQVIA","channel":"상급종병","period":"previous_year"},'
            '"no_data_flag":false,"confidence":0.91,"reason":"매출 조건"}'
        )
    )

    routes = router.route("리바로 작년 상급종병 M/S IQVIA 기준")

    assert routes[0].sources == ("metrics",)
    assert routes[0].filters == (
        ("channel", "상급종병"),
        ("period", "previous_year"),
        ("source", "IQVIA"),
    )
    assert router.last_diagnostics.mode == "llm"


def test_llm_router_accepts_llm_only_metric_filter() -> None:
    router = LLMFirstBQRouter(
        decomposer=FakeDecomposer(
            '{"bq_ids":["Q1"],"tools":["metrics"],"brands":["리바로"],'
            '"filters":{"source":"IQVIA","channel":"상급종병"},'
            '"no_data_flag":false,"confidence":0.91,"reason":"매출 조건"}'
        )
    )

    routes = router.route("리바로 기준 알려줘")

    assert routes[0].sources == ("metrics",)
    assert routes[0].filters == (("channel", "상급종병"), ("source", "IQVIA"))


def test_llm_router_accepts_hira_disease_external_route() -> None:
    router = LLMFirstBQRouter(
        decomposer=FakeDecomposer(
            '{"bq_ids":["Q1"],"tools":["external_api"],"brands":["리바로"],'
            '"no_data_flag":false,"confidence":0.96,"reason":"질병 환자수"}'
        )
    )

    routes = router.route("리바로 관련 질병 환자수")

    assert routes[0].bq == "Q1"
    assert routes[0].sources == ("external_api",)
    assert router.last_diagnostics.mode == "llm"


def test_llm_router_preserves_no_data_boundary_even_if_llm_would_route_metrics() -> None:
    router = LLMFirstBQRouter(
        decomposer=FakeDecomposer(
            '{"bq_ids":["Q1"],"tools":["metrics"],"brands":["리바로"],'
            '"no_data_flag":false,"confidence":0.99}'
        )
    )

    routes = router.route("리바로 포트폴리오 사업성은?")

    assert routes[0].bq == "Q5"
    assert routes[0].sources == ("none",)
    assert router.last_diagnostics.mode == "guard"


def test_llm_router_preserves_business_feasibility_boundary() -> None:
    router = LLMFirstBQRouter(
        decomposer=FakeDecomposer(
            '{"bq_ids":["Q1"],"tools":["metrics"],"brands":["리바로"],'
            '"no_data_flag":false,"confidence":0.99}'
        )
    )

    routes = router.route("리바로 신사업 타당성은?")

    assert routes[0].bq == "Q5"
    assert routes[0].sources == ("none",)
    assert router.last_diagnostics.mode == "guard"


def test_llm_router_normalizes_uploaded_document_q1_q5_alias() -> None:
    router = LLMFirstBQRouter(
        decomposer=FakeDecomposer(
            '{"bq_ids":["Q1","Q5"],"tools":["document"],"brands":[],'
            '"no_data_flag":false,"confidence":0.99,"reason":"업로드 문서 기반 전망"}'
        )
    )

    routes = router.route("업로드한 가이드라인 기반 시장 전망", has_documents=True)

    assert [route.bq for route in routes] == ["Q1/Q5"]
    assert routes[0].sources == ("document",)
    assert router.last_diagnostics.mode == "llm"


def test_chat_agent_uses_llm_router_when_structured_output_is_valid() -> None:
    router = LLMFirstBQRouter(
        decomposer=FakeDecomposer(
            '{"bq_ids":["Q1"],"tools":["metrics"],"brands":["리바로"],'
            '"no_data_flag":false,"confidence":0.88,"reason":"모호하지만 시장성과"}'
        )
    )

    result = ChatAgent(router=router).answer("리바로 잘나가?")

    assert result["router_diagnostics"]["mode"] == "llm"
    assert "cache" in result["sources"]
    assert "deep_analysis_events" not in result["sources"]
    assert any(call["tool"] == "get_market_landscape" for call in result["tool_calls"])


def test_chat_agent_falls_back_to_keyword_router_when_llm_unavailable() -> None:
    router = LLMFirstBQRouter(decomposer=FailingDecomposer())

    result = ChatAgent(router=router).answer("리바로 임상 현황")

    assert result["router_diagnostics"]["fallback_used"] is True
    assert "external_api" in result["sources"]
