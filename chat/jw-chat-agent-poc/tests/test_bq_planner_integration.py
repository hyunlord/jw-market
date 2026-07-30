from __future__ import annotations

from threading import Lock
import time

from jw_chat_agent_poc import ChatAgent
from jw_chat_agent_poc.agent_loop.loop import ToolUseAgent
from jw_chat_agent_poc.agent_loop.requested_source import source_domain_note
from jw_chat_agent_poc.agent_loop.tools import AgentToolFacade, ToolExecution
from jw_chat_agent_poc.orchestrator.market_answer_contract import enforce_market_answer_contract
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.resolver.catalog_membership import (
    StaticCatalogMembershipReader,
    TtlCatalogMembershipReader,
)
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.metrics.cache_live import StaticMetricsCacheReader
from jw_chat_agent_poc.tools.query_layer import MartRecord, StaticStrategicMartReader, StrategicQueryLayer


def test_bq_plan_executes_both_market_sources_without_llm() -> None:
    layer = _layer()
    agent = ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=BrandResolver(mode="fixture"),
        query_layer=layer,
    )

    result = agent.answer("리바로 IQVIA랑 UBIST 수치가 다른데 왜?")

    metrics = result["agent_loop_metrics"]
    assert metrics["deterministic_plan_hit"] is True
    assert metrics["deterministic_plan_kind"] == "BQ:C3"
    assert metrics["llm_plan_calls"] == 0
    series_calls = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric"
        and call.get("render_data", {}).get("metric") == "series"
    ]
    assert [call["render_data"]["query_spec"]["source"] for call in series_calls] == [
        "ubist",
        "iqvia_nsa",
    ]
    analysis = next(call for call in result["tool_calls"] if call.get("tool") == "bq_analysis")
    assert analysis["render_data"]["never_aggregate_sources"] is True
    assert analysis["qa_trace"]["started_at"]
    assert analysis["qa_trace"]["ended_at"]
    assert analysis["qa_trace"]["status"] == "ok"
    assert analysis["qa_trace"]["row_count"] > 0
    assert "합산하지" in result["answer"]


def test_multiple_brand_bq_preflight_asks_for_one_brand_instead_of_narrowing() -> None:
    agent = ToolUseAgent(
        metrics=MetricsTool(mode="fixture"),
        resolver=BrandResolver(mode="fixture"),
    )

    result = agent.answer("리바로와 리바로젯 매출 알려줘")

    assert result["decomposition"] == [
        {
            "intent": "brand_cardinality_clarification",
            "status": "needs_clarification",
            "max_steps": 0,
        }
    ]
    assert result["router_diagnostics"]["gate"] == "bq_brand_cardinality"
    assert result["router_diagnostics"]["gate_reason"] == (
        "multiple_brands_require_cardinality_contract"
    )
    assert result["agent_loop_metrics"]["status"] == "needs_clarification"
    assert result["agent_loop_metrics"]["tool_calls"] == 0
    assert result["tool_calls"] == []
    assert "리바로, 리바로젯" in result["answer"]
    assert "한 브랜드를 지정" in result["answer"]


def test_available_sources_is_unknown_without_query_catalog() -> None:
    facade = AgentToolFacade(
        metrics=MetricsTool(mode="fixture"),
        resolver=BrandResolver(mode="fixture"),
    )

    assert facade.available_sources() is None


def test_bq_question_bypasses_llm_question_router() -> None:
    class RouterBomb:
        def route(self, _question: str, has_documents: bool = False):
            raise AssertionError("BQ question must bypass LLM decomposition")

    layer = _layer()
    agent = ChatAgent(
        router=RouterBomb(),
        resolver=BrandResolver(mode="fixture"),
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        query_layer=layer,
    )

    result = agent.answer("리바로 IQVIA랑 UBIST 수치가 다른데 왜?")

    assert result["agent_loop_metrics"]["deterministic_plan_kind"] == "BQ:C3"
    assert result["router_diagnostics"]["question_decomposition_bypassed"] is True


def test_bq_preflight_returns_typed_ambiguity_for_product_family() -> None:
    class RouterBomb:
        def route(self, _question: str, has_documents: bool = False):
            raise AssertionError("ambiguous family must stop before routing")

    memberships = TtlCatalogMembershipReader(
        StaticCatalogMembershipReader(
            tuple(
                {
                    "brand": brand,
                    "market_id": "",
                    "market_name": "",
                    "support_source": "general_mart",
                }
                for brand in ("카나브", "카나브젯", "카나브플러스")
            )
        )
    )
    resolver = BrandResolver(
        mode="cache",
        brand_reader=StaticMetricsCacheReader(cache_brands=[], market_status={}),
        membership_reader=memberships,
    )
    layer = _layer()
    agent = ChatAgent(
        router=RouterBomb(),
        resolver=resolver,
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        query_layer=layer,
    )

    result = agent.answer("카나브패밀리 실적 어때?")

    assert result["sources"] == ["ambiguous_brand"]
    assert result["tool_calls"] == []
    assert all(candidate in result["answer"] for candidate in ("카나브", "카나브젯", "카나브플러스"))
    assert "하나를 지정" in result["answer"]


def test_bq_independent_support_tools_execute_in_parallel(monkeypatch) -> None:
    layer = _layer()
    agent = ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=BrandResolver(mode="fixture"),
        query_layer=layer,
    )
    support_tools = {"search_news", "get_disease_stats", "csd_activity_trend"}
    state = {"active": 0, "peak": 0}
    lock = Lock()

    def execute(_facade, plan):
        if plan.name in support_tools:
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.03)
            with lock:
                state["active"] -= 1
        return ToolExecution(
            "ok",
            plan.name,
            {"source": plan.name, "tool": plan.name, "render_data": {"brand": "리바로"}},
            plan.arguments,
        )

    monkeypatch.setattr("jw_chat_agent_poc.agent_loop.loop._execute_grounded", execute)

    result = agent.answer("리바로 점유율이 왜 이렇게 됐어?")

    assert state["peak"] >= 2
    assert result["agent_loop_metrics"]["llm_plan_calls"] == 0
    tool_stages = [item for item in result["timing"]["stages"] if item["name"].startswith("tool:")]
    assert any("mode=parallel" in item["detail"] for item in tool_stages)


def test_invalid_bq_analysis_fails_closed_before_response_assembly(monkeypatch) -> None:
    layer = _layer()
    agent = ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=BrandResolver(mode="fixture"),
        query_layer=layer,
    )
    monkeypatch.setattr(
        "jw_chat_agent_poc.agent_loop.loop.build_bq_analysis_call",
        lambda _contract, _calls: {
            "tool": "bq_analysis",
            "source": "BQ deterministic evidence",
            "summary_text": "document_id=42",
            "render_data": {
                "contract_id": "C3",
                "calculation": "source_divergence",
                "insights": ["document_id=42"],
                "source_labels": ["UBIST", "IQVIA NSA"],
                "never_aggregate_sources": True,
            },
        },
    )

    result = agent.answer("리바로 IQVIA랑 UBIST 수치가 다른데 왜?")

    assert not any(call.get("tool") == "bq_analysis" for call in result["tool_calls"])
    assert result["agent_loop_metrics"]["bq_analysis_validation"] == "VERIFICATION_FAIL"
    assert result["agent_loop_metrics"]["status"] == "verification_failed"


def test_missing_required_bq_source_fails_closed() -> None:
    layer = _layer(("ubist",))
    agent = ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=BrandResolver(mode="fixture"),
        query_layer=layer,
    )

    result = agent.answer("리바로 IQVIA랑 UBIST 수치가 다른데 왜?")

    assert not any(call.get("tool") == "bq_analysis" for call in result["tool_calls"])
    assert result["agent_loop_metrics"]["status"] == "source_unavailable"
    assert result["agent_loop_metrics"]["bq_analysis_validation"] == "SOURCE_UNAVAILABLE"
    assert result["agent_loop_metrics"]["bq_missing_sources"] == ["iqvia_nsa"]
    assert "IQVIA NSA" in result["answer"]


def test_missing_bq_source_basis_notice_survives_market_contract() -> None:
    layer = _layer(("ubist",))
    agent = ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=BrandResolver(mode="fixture"),
        query_layer=layer,
    )
    question = "리바로 IQVIA랑 UBIST 수치가 다른데 왜?"

    result = agent.answer(question)
    expected_notice = source_domain_note(("iqvia_nsa",))
    assert expected_notice is not None
    assert expected_notice in result["answer"]

    contracted = enforce_market_answer_contract(
        question,
        result["answer"],
        result["tool_calls"],
    )

    assert expected_notice in contracted


def test_missing_bq_analysis_output_fails_closed(monkeypatch) -> None:
    layer = _layer()
    agent = ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=BrandResolver(mode="fixture"),
        query_layer=layer,
    )
    monkeypatch.setattr(
        "jw_chat_agent_poc.agent_loop.loop.build_bq_analysis_call",
        lambda _contract, _calls: None,
    )

    result = agent.answer("리바로 IQVIA랑 UBIST 수치가 다른데 왜?")

    assert not any(call.get("tool") == "bq_analysis" for call in result["tool_calls"])
    assert result["agent_loop_metrics"]["status"] == "verification_failed"
    assert result["agent_loop_metrics"]["bq_analysis_validation"] == "MISSING_EVIDENCE"


def _layer(sources: tuple[str, ...] = ("ubist", "iqvia_nsa")) -> StrategicQueryLayer:
    records = tuple(
        MartRecord(
            ml_id="ml_006",
            brand_name="리바로",
            source=source,
            measure="sales",
            metric_history={
                "2026-04": {"raw_value": start, "ms": share, "source_status": "OK"},
                "2026-05": {"raw_value": end, "ms": share + 0.1, "source_status": "OK"},
            },
            channel_data={},
            specialty_data={},
            dimension_data={},
            by_dimension={"company": "JW중외제약", "molecule": "pitavastatin"},
        )
        for source, start, end, share in (
            ("ubist", 8_000_000_000.0, 8_100_000_000.0, 3.7),
            ("iqvia_nsa", 8_300_000_000.0, 8_500_000_000.0, 3.9),
        )
        if source in sources
    )
    return StrategicQueryLayer(reader=StaticStrategicMartReader(records))
