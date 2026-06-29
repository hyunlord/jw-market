from __future__ import annotations

from dataclasses import dataclass

from jw_chat_agent_poc import ChatAgent
from jw_chat_agent_poc.agent_loop.loop import ToolUseAgent
from jw_chat_agent_poc.agent_loop.models import AgentDecision, ToolCallPlan
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.router import BQRouter
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.metrics.cache_live import StaticCausePayloadReader, StaticMetricsCacheReader

from test_metrics_cache import BRAND_CARDS, CACHE_BRANDS, CAUSE_PAYLOAD
from test_metrics_relative_dates import CAUSE_PAYLOAD as RELATIVE_CAUSE_PAYLOAD


COMPETITOR_CAUSE_PAYLOAD = {
    "data": {
        **CAUSE_PAYLOAD["data"],
        "level_top5_trend": {
            "by_level": {
                "Brand": {
                    "periods_10pt": ["2025-01", "2025-12", "2026-04"],
                    "values": [
                        {
                            "brands_in_value": [
                                {
                                    "brand": "리바로젯",
                                    "value_series_10pt": [12_000_000_000.0, 20_000_000_000.0, 12_009_054_192.93],
                                    "ms_series_10pt": [12.0, 13.3333, 5.3213],
                                    "rank_series_10pt": [2, 2, 3],
                                }
                            ]
                        }
                    ],
                }
            }
        },
    }
}


@dataclass(slots=True)
class ScriptedPlanner:
    decisions: tuple[AgentDecision, ...]
    index: int = 0
    allowed_brand_history: list[tuple[str, ...]] | None = None
    allowed_period_history: list[tuple[str, ...]] | None = None
    schema_history: list[tuple[dict, ...]] | None = None

    def decide(self, question, observations, schemas, allowed_brands=(), allowed_periods=()):
        if self.allowed_brand_history is None:
            self.allowed_brand_history = []
        if self.allowed_period_history is None:
            self.allowed_period_history = []
        if self.schema_history is None:
            self.schema_history = []
        self.allowed_brand_history.append(tuple(allowed_brands))
        self.allowed_period_history.append(tuple(allowed_periods))
        self.schema_history.append(tuple(schemas))
        decision = self.decisions[min(self.index, len(self.decisions) - 1)]
        self.index += 1
        return decision


def _metrics_tool(cause_payload=CAUSE_PAYLOAD) -> MetricsTool:
    cache_reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    cause_reader = StaticCausePayloadReader(
        {
            ("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): cause_payload,
            ("리바로젯", "market_landscape", "UBIST", "sales", "strategy_006"): COMPETITOR_CAUSE_PAYLOAD,
        }
    )
    return MetricsTool(mode="cache", cache_reader=cache_reader, cause_reader=cause_reader)


def _brand_enum(schemas: tuple[dict, ...], tool_name: str) -> list[str]:
    schema = next(item for item in schemas if item["function"]["name"] == tool_name)
    return schema["function"]["parameters"]["properties"]["brand"]["enum"]


def test_agent_loop_combines_market_scope_and_metric_tools_for_largest_competitor() -> None:
    # Given: a scripted LLM planner that asks for market scope, then member-brand metrics.
    planner = ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(name="get_market_scope", arguments={"brand": "리바로", "view": "market_landscape"}, reason="시장 범위를 먼저 확인"),
                )
            ),
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "sales", "period": "previous_year"}, reason="기준 브랜드 작년 매출"),
                    ToolCallPlan(name="get_metric", arguments={"brand": "리바로젯", "measure": "sales", "period": "previous_year"}, reason="같은 시장 경쟁 브랜드 작년 매출"),
                )
            ),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    metrics = _metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner),
    )

    # When: a complex same-market competitor question is asked.
    result = agent.answer("리바로 같은 시장에서 작년 제일 큰 경쟁사는?")

    # Then: the new agent path combines market and metric tools, with verified evidence.
    assert result["decomposition"][0]["intent"] == "agent_loop"
    assert [call["tool"] for call in result["tool_calls"]].count("get_market_landscape") == 1
    assert [call["render_data"].get("brand") for call in result["tool_calls"] if call["tool"] == "get_brand_metric"] == ["리바로", "리바로젯"]
    assert any(call["tool"] == "agent_calculation" for call in result["tool_calls"])
    assert "리바로젯" in result["answer"]
    assert "200.00억원" in result["answer"]
    assert "## 근거" in result["answer"]
    assert result["agent_loop_metrics"]["steps"] == 3
    assert planner.allowed_brand_history is not None
    assert planner.allowed_brand_history[0] == ("리바로",)
    assert "리바로젯" in planner.allowed_brand_history[1]
    assert planner.schema_history is not None
    assert _brand_enum(planner.schema_history[0], "get_market_scope") == ["리바로"]
    assert "리바로젯" in _brand_enum(planner.schema_history[1], "get_metric")


def test_agent_loop_completes_missing_competitor_metric_for_largest_competitor() -> None:
    # Given: the live planner picks the right tool groups but stops after the anchor brand metric.
    planner = ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(name="get_market_scope", arguments={"brand": "리바로", "view": "market_landscape"}, reason="시장 범위 확인"),
                )
            ),
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "sales", "period": "previous_year"}, reason="기준 브랜드 작년 매출"),
                )
            ),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    metrics = _metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner),
    )

    # When: a largest-competitor calculation needs the same-market member metric.
    result = agent.answer("리바로 같은 시장에서 작년 제일 큰 경쟁사는?")

    # Then: the verification layer completes the missing member metric before calculating.
    metric_brands = [call["render_data"].get("brand") for call in result["tool_calls"] if call["tool"] == "get_brand_metric"]
    assert metric_brands == ["리바로", "리바로젯"]
    completed = next(call for call in result["tool_calls"] if call["tool"] == "get_brand_metric" and call["render_data"].get("brand") == "리바로젯")
    assert completed["render_data"]["completion_reason"] == "largest_competitor_requires_member_metric"
    assert any(call["tool"] == "agent_calculation" for call in result["tool_calls"])
    assert "리바로젯" in result["answer"]


def test_agent_loop_backfills_ranking_metric_when_planner_returns_no_tool_calls() -> None:
    # Given: the planner produces a valid final response shell without calling the ranking metric tool.
    planner = ScriptedPlanner((AgentDecision(final_answer="도구 결과로 답변하세요."),))
    metrics = _metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner),
    )

    # When: a ranking/share question asks for required rank facts.
    result = agent.answer("리바로 점유율 몇 위야")

    # Then: AnswerContract backfills the metric call deterministically instead of returning an empty shell.
    metric_calls = [call for call in result["tool_calls"] if call["tool"] == "get_brand_metric"]
    assert metric_calls
    data = metric_calls[0]["render_data"]
    assert data["brand"] == "리바로"
    assert data["rank"] == 6
    assert data["total_brands_in_market"] == 516
    assert data["completion_reason"] == "answer_contract_requires_ranking_facts"
    assert "순위 6/516" in result["markdown_response"]["fact_md"]
    assert result["agent_loop_metrics"]["tool_selection_accuracy"] == 1.0


def test_agent_loop_corrects_invalid_llm_brand_to_pre_resolved_canonical() -> None:
    # Given: a planner emits the spike failure brand typo even though the question says 리바로.
    planner = ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(name="get_market_scope", arguments={"brand": "리바트", "view": "market_landscape"}, reason="LLM typo"),
                )
            ),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    metrics = _metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner),
    )

    # When: the Stage 2 spike question is asked.
    result = agent.answer("리바로 같은 시장에서 작년 제일 큰 경쟁사는?")

    # Then: the executed tool argument is grounded to the resolver's canonical brand.
    assert result["resolution"]["canonical_brand"] == "리바로"
    assert result["agent_trace"][0]["observations"][0]["arguments"]["brand"] == "리바로"
    assert result["tool_calls"][0]["render_data"]["anchor_brand"] == "리바로"
    assert "리바트" not in result["answer"]
    assert planner.allowed_brand_history == [("리바로",), ("리바로", "리바로젯")]


def test_agent_loop_rejects_valid_but_out_of_context_brand_argument() -> None:
    # Given: a planner tries to leave the pre-resolved brand set for a 리바로 question.
    planner = ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(name="get_metric", arguments={"brand": "가드렛", "measure": "sales", "period": "previous_year"}, reason="wrong market"),
                )
            ),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    metrics = _metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner),
    )

    # When: the planner's brand argument conflicts with code grounding.
    result = agent.answer("리바로 3달전 대비 점유율 변화?")

    # Then: the invalid tool argument is fail-closed rather than querying another brand.
    assert result["tool_calls"][0]["tool"] == "unsupported_metric"
    assert result["tool_calls"][0]["render_data"]["arguments"]["brand"] == "가드렛"
    assert "allowed canonical brand" in result["tool_calls"][0]["summary_text"]


def test_agent_loop_combines_relative_date_and_metric_tools_for_share_delta() -> None:
    # Given: a planner that resolves a relative date and fetches both comparison periods.
    planner = ScriptedPlanner(
        (
            AgentDecision(tool_calls=(ToolCallPlan(name="resolve_relative_date", arguments={"expression": "3달전"}, reason="비교 기간 해석"),)),
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "market_share", "period": "2026-03"}, reason="3달전 점유율"),
                    ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "market_share", "period": "latest"}, reason="최신 점유율"),
                )
            ),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    metrics = _metrics_tool(RELATIVE_CAUSE_PAYLOAD)
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner, current_month=lambda: "2026-06"),
    )

    # When: a relative comparison question is asked.
    result = agent.answer("리바로 3달전 대비 점유율 변화?")

    # Then: date resolution and two metric calls feed a verified delta calculation.
    assert result["decomposition"][0]["intent"] == "agent_loop"
    assert any(call["tool"] == "resolve_relative_date" for call in result["tool_calls"])
    delta_call = next(call for call in result["tool_calls"] if call["tool"] == "agent_calculation")
    assert delta_call["render_data"]["ms_delta_pct"] == -0.0433
    assert "2026-03" in result["answer"]
    assert "-0.04%" in result["answer"]
    assert "## 근거" in result["answer"]


def test_simple_sales_question_keeps_single_shot_path() -> None:
    # Given: the agent loop is available.
    metrics = _metrics_tool()
    agent = ChatAgent(router=BQRouter(), metrics=metrics, agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver()))

    # When: a simple sales question is asked.
    result = agent.answer("리바로 작년 매출")

    # Then: the existing deterministic single-shot path is preserved.
    assert result["decomposition"][0].get("intent") != "agent_loop"
    assert result["tool_calls"][0]["tool"] == "get_brand_metric"
    assert not any(call["tool"] == "deep_analysis_related_news" for call in result["tool_calls"])
    assert "160.00억원" in result["answer"]


def test_agent_loop_stops_duplicate_tool_calls_without_spinning() -> None:
    # Given: the planner repeats the exact same tool call.
    repeated = AgentDecision(
        tool_calls=(ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "sales", "period": "latest"}, reason="반복 호출"),)
    )
    planner = ScriptedPlanner((repeated, repeated, AgentDecision(final_answer="should not matter")))
    metrics = _metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner),
    )

    # When: a complex question triggers the loop.
    result = agent.answer("리바로 3달전 대비 점유율 변화?")

    # Then: duplicate-state detection stops the loop and returns a transparent partial answer.
    assert result["agent_loop_metrics"]["status"] == "duplicate_stopped"
    assert result["agent_loop_metrics"]["steps"] == 2
    assert "반복 도구 호출" in result["answer"]
