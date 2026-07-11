from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jw_chat_agent_poc import ChatAgent
from jw_chat_agent_poc.agent_loop import should_use_agent_loop
from jw_chat_agent_poc.agent_loop.loop import ToolUseAgent, _sales_delta_calls
from jw_chat_agent_poc.agent_loop.models import AgentDecision, ToolCallPlan
from jw_chat_agent_poc.agent_loop.external_tools import _web_search_query, clinical_call
from jw_chat_agent_poc.agent_loop.planner import GenosToolPlanner, HeuristicToolPlanner, select_candidate_tools
from jw_chat_agent_poc.orchestrator.answer_contract import enforce_answer_contract
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.router import BQRouter
from jw_chat_agent_poc.tools.deep_analysis.news import DeepAnalysisNewsTool, StaticDeepAnalysisNewsReader
from jw_chat_agent_poc.tools.external import ExternalApiClient, ExternalCall
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.metrics.cache_live import StaticCausePayloadReader, StaticMetricsCacheReader

from test_metrics_cache import BRAND_CARDS, CACHE_BRANDS, CAUSE_PAYLOAD, cause_payload_with_top_brand_trends
from test_query_layer_integration import _query_layer


@dataclass(slots=True)
class Stage3ScriptedPlanner:
    decisions: tuple[AgentDecision, ...]
    index: int = 0
    allowed_brand_history: list[tuple[str, ...]] | None = None
    allowed_period_history: list[tuple[str, ...]] | None = None
    schema_history: list[tuple[dict[str, Any], ...]] | None = None

    def decide(
        self,
        _question: str,
        _observations,
        schemas: tuple[dict[str, Any], ...],
        allowed_brands: tuple[str, ...] = (),
        allowed_periods: tuple[str, ...] = (),
    ) -> AgentDecision:
        if self.allowed_brand_history is None:
            self.allowed_brand_history = []
        if self.allowed_period_history is None:
            self.allowed_period_history = []
        if self.schema_history is None:
            self.schema_history = []
        self.allowed_brand_history.append(tuple(allowed_brands))
        self.allowed_period_history.append(tuple(allowed_periods))
        self.schema_history.append(schemas)
        decision = self.decisions[min(self.index, len(self.decisions) - 1)]
        self.index += 1
        return decision


@dataclass(frozen=True, slots=True)
class _ExternalResolution:
    canonical_brand: str
    molecule_en: tuple[str, ...]
    is_combo: bool = False


class _ClinicalBroadExternal:
    def clinicaltrials_v2_search(self, query_intr: str) -> ExternalCall:
        return ExternalCall(
            tool="clinicaltrials_v2_search",
            source="external_api",
            status="live",
            summary_text=f"ClinicalTrials MCP matched {query_intr}",
            render_data={
                "payload": {
                    "studies": [
                        {
                            "protocolSection": {
                                "identificationModule": {"nctId": "NCT05537948", "briefTitle": "Pitavastatin liver transplant study"},
                                "statusModule": {"overallStatus": "ACTIVE_NOT_RECRUITING"},
                                "designModule": {"phases": ["PHASE4"]},
                            }
                        }
                    ]
                }
            },
        )

    def mfds_clinical_trial_kr(self, _keyword: str) -> ExternalCall:
        return ExternalCall(
            tool="mfds_clinical_trial_kr",
            source="external_api",
            status="live",
            summary_text="mfds_clinical_trial_kr returned HTTP 200, totalCount=14039",
            render_data={
                "items": [
                    {"GOODS_NAME": "CJ-20001", "CLINC_EXAM_TITLE": "급성 위염 임상", "CLNC_TEST_SN": "201002160"},
                    {"GOODS_NAME": "GSK2402968", "CLINC_EXAM_TITLE": "DMD 임상", "CLNC_TEST_SN": "201101010"},
                ]
            },
        )


def _metrics_tool() -> MetricsTool:
    cache_reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    cause_reader = StaticCausePayloadReader(
        {
            ("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): CAUSE_PAYLOAD,
        }
    )
    return MetricsTool(mode="cache", cache_reader=cache_reader, cause_reader=cause_reader)


def _livalohigh_metrics_tool() -> MetricsTool:
    cache_reader = StaticMetricsCacheReader(
        cache_brands=[{"brand": "리바로하이", "market_id": "strategy_011", "market_name": "리바로하이 시장", "sources": ["UBIST"]}],
        market_status={
            "brand_cards": [
                {
                    "brand": "리바로하이",
                    "market_id": "strategy_011",
                    "market_name": "리바로하이 시장",
                    "rank": 4,
                    "total_brands_in_market": 18,
                    "front": {"value_recent": 3_100_000_000.0, "ms_recent_pct": 4.1, "default_source": "UBIST"},
                    "back": {"cagr_5y_pct": 2.3},
                    "back_extended": {"market_size_recent": 75_000_000_000.0, "market_cagr_5y_pct": 3.5, "excess_growth_pct": -1.2},
                }
            ],
            "kpi_summary": {"UBIST": {"period_recent": "2026-04"}},
        },
    )
    return MetricsTool(mode="cache", cache_reader=cache_reader, cause_reader=StaticCausePayloadReader({}))


def _news_tool() -> DeepAnalysisNewsTool:
    return DeepAnalysisNewsTool(
        reader=StaticDeepAnalysisNewsReader(
            {
                "리바로": {
                    "data": {
                        "events": [
                            {
                                "date": "2026-04-12",
                                "title": "아토젯 약가 이슈 이후 리바로 처방 동향",
                                "source": "약업신문",
                                "url": "https://news.example/atozet-livalo",
                                "impact_score": 82,
                                "on_list": True,
                                "summary": "아토젯 이슈와 리바로 시장 반응을 함께 다룬 기사",
                                "body_full": "아토젯 이슈 이후 리바로 매출 변화가 언급됐다.",
                            }
                        ]
                    }
                }
            }
        )
    )


def _resolver_with_atozet(tmp_path) -> BrandResolver:
    fixture_path = tmp_path / "brand_catalog.json"
    base_path = Path(__file__).resolve().parents[1] / "jw_chat_agent_poc" / "fixtures" / "brand_catalog.json"
    catalog = json.loads(base_path.read_text(encoding="utf-8"))
    catalog.append(
        {
            "canonical_brand": "아토젯",
            "aliases": ["atozet", "ATOZET"],
            "audit_code": "test_atozet",
            "molecule_en": ["atorvastatin", "ezetimibe"],
            "atc": ["C10C0"],
            "edi_code": None,
            "item_seq": None,
        }
    )
    fixture_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    return BrandResolver(fixture_path=fixture_path)


def _metrics_tool_with_atozet() -> MetricsTool:
    cards = copy.deepcopy(BRAND_CARDS)
    cards["brand_cards"].append(
        {
            "rank": 1,
            "total_brands_in_market": 516,
            "brand": "아토젯",
            "market_id": "strategy_006",
            "market_name": "리바로/리바로젯",
            "front": {"value_recent": 26_100_000_000.0, "ms_recent_pct": 11.56, "default_source": "UBIST"},
            "back": {"cagr_5y_pct": 9.4},
            "back_extended": {
                "market_size_recent": 225_677_368_890.97986,
                "market_cagr_5y_pct": 16.18,
                "brand_cagr_5y_pct": 9.4,
                "excess_growth_pct": -6.78,
                "source_label": "UBIST",
                "market_label_kor": "고지혈증",
            },
        }
    )
    brands = [*CACHE_BRANDS, {"brand": "아토젯", "market_id": "strategy_006", "sources": ["UBIST"], "rank": 1}]
    atozet_payload = copy.deepcopy(CAUSE_PAYLOAD)
    brand_series = atozet_payload["data"]["level_top5_trend"]["by_level"]["Brand"]["values"][0]["brands_in_value"][0]
    brand_series["brand"] = "아토젯"
    brand_series["value_series_10pt"] = [24_900_000_000.0, 25_800_000_000.0, 25_600_000_000.0, 26_100_000_000.0]
    brand_series["ms_series_10pt"] = [11.1, 11.4, 11.2, 11.56]
    brand_series["rank_series_10pt"] = [1, 1, 1, 1]
    brand_series["value_recent"] = 26_100_000_000.0
    brand_series["ms_recent_pct"] = 11.56
    brand_series["rank"] = 1
    cause_reader = StaticCausePayloadReader(
        {
            ("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): CAUSE_PAYLOAD,
            ("아토젯", "market_landscape", "UBIST", "sales", "strategy_006"): atozet_payload,
        }
    )
    return MetricsTool(mode="cache", cache_reader=StaticMetricsCacheReader(cache_brands=brands, market_status=cards), cause_reader=cause_reader)


def _metrics_tool_with_atozet_segment_only() -> MetricsTool:
    payload = copy.deepcopy(CAUSE_PAYLOAD)
    payload["data"]["analysis_levels"]["data"]["Brand"]["ms_segments"] = [
        {"name": "로수젯", "rank": 1, "recent_share_pct": 9.1659, "value_recent": 20_685_385_934.33},
        {"name": "아토젯", "rank": 4, "recent_share_pct": 5.1620, "value_recent": 11_648_132_500.0},
        {"name": "로수바미브", "rank": 5, "recent_share_pct": 4.2897, "value_recent": 9_681_501_337.12},
    ]
    cause_reader = StaticCausePayloadReader(
        {
            ("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): payload,
        }
    )
    return MetricsTool(
        mode="cache",
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS),
        cause_reader=cause_reader,
    )


def _metrics_tool_with_atozet_trend_only() -> MetricsTool:
    payload = cause_payload_with_top_brand_trends()
    cause_reader = StaticCausePayloadReader(
        {
            ("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): payload,
        }
    )
    return MetricsTool(
        mode="cache",
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS),
        cause_reader=cause_reader,
    )


def _period_enum(schemas: tuple[dict[str, Any], ...], tool_name: str) -> list[str]:
    schema = next(item for item in schemas if item["function"]["name"] == tool_name)
    return schema["function"]["parameters"]["properties"]["period"]["enum"]


def _tool_names(result: dict[str, Any]) -> list[str]:
    return [str(call.get("tool")) for call in result["tool_calls"]]


def test_period_grounding_exposes_available_enum_and_blocks_2026_06() -> None:
    planner = Stage3ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "market_share", "period": "2026-06"}, reason="bad unavailable month"),
                )
            ),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    payload = cause_payload_with_top_brand_trends()
    payload["data"]["sources_data"]["market_size_series"] = {
        "2026-01": {"value": 230_000_000_000.0, "yoy_growth_pct": 21.0},
        "2026-02": {"value": 215_000_000_000.0, "yoy_growth_pct": 12.0},
        "2026-03": {"value": 228_838_670_570.0, "yoy_growth_pct": 25.59},
        "2026-04": {"value": 225_677_368_890.97986, "yoy_growth_pct": 36.88},
    }
    brand_block = payload["data"]["level_top5_trend"]["by_level"]["Brand"]
    brand_block["periods_10pt"] = ["2026-01", "2026-02", "2026-03", "2026-04"]
    livalo_row = brand_block["values"][0]["brands_in_value"][0]
    livalo_row["value_series_10pt"] = [9_000_000_000.0, 8_000_000_000.0, 8_711_248_139.54, 8_493_234_217.11]
    livalo_row["ms_series_10pt"] = [3.91, 3.72, 3.8067, 3.7634]
    metrics = MetricsTool(
        mode="cache",
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS),
        cause_reader=StaticCausePayloadReader({("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): payload}),
    )
    agent = ChatAgent(router=BQRouter(), metrics=metrics, agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner))

    result = agent.answer("리바로 3달전 대비 점유율 변화?")

    assert planner.allowed_period_history is not None
    assert "2026-04" in planner.allowed_period_history[0]
    assert "2026-06" not in planner.allowed_period_history[0]
    assert planner.schema_history is not None
    assert "2026-06" not in _period_enum(planner.schema_history[0], "get_metric")
    assert result["tool_calls"][0]["tool"] == "unsupported_metric"
    assert "available period enum" in result["tool_calls"][0]["summary_text"]
    assert all(call.get("render_data", {}).get("period") != "2026-06" for call in result["tool_calls"])


def test_share_delta_completion_fetches_metrics_when_planner_only_resolves_date() -> None:
    planner = Stage3ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(name="resolve_relative_date", arguments={"expression": "3달전"}, reason="비교 기간 확인"),
                    ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "market_share", "period": "2026-01"}, reason="잘못 고른 비교 월"),
                )
            ),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    metrics = _metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner, current_month=lambda: "2026-06"),
    )

    result = agent.answer("리바로 3달전 대비 점유율 변화")

    periods = [
        call.get("render_data", {}).get("period")
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric"
    ]
    calculations = [
        call.get("render_data", {})
        for call in result["tool_calls"]
        if call.get("tool") == "agent_calculation"
    ]
    assert "2026-03" in periods
    assert "2026-04" in periods
    assert any(item.get("metric") == "market_share_delta" and item.get("period") == "2026-03→2026-04" for item in calculations)
    assert "점유율 변화" in result["markdown_response"]["fact_md"]
    assert "필수 답변 fact" in result["markdown_response"]["fact_md"]
    assert "2026-03→2026-04" in result["markdown_response"]["fact_md"]


def test_share_delta_calculation_uses_series_when_planner_returns_series_metric() -> None:
    planner = Stage3ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(name="resolve_relative_date", arguments={"expression": "3달전"}, reason="비교 기간 확인"),
                    ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "series", "period": "latest"}, reason="시계열 확인"),
                )
            ),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    metrics = _metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner, current_month=lambda: "2026-06"),
    )

    result = agent.answer("리바로 3달전 대비 점유율 변화")

    calculations = [
        call.get("render_data", {})
        for call in result["tool_calls"]
        if call.get("tool") == "agent_calculation"
    ]
    assert any(
        item.get("metric") == "market_share_delta"
        and item.get("period") == "2026-03→2026-04"
        and item.get("ms_delta_pct") == -0.0433
        for item in calculations
    )
    assert "점유율 변화" in result["markdown_response"]["fact_md"]


def test_complex_news_and_sales_question_uses_news_and_metric_tools() -> None:
    planner = Stage3ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(name="search_news", arguments={"brand": "리바로", "query": "아토젯"}, reason="뉴스 이슈 확인"),
                    ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "sales", "period": "latest"}, reason="매출 확인"),
                )
            ),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    metrics = _metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        news=_news_tool(),
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner, news=_news_tool()),
    )

    result = agent.answer("리바로 뉴스에서 아토젯 이슈랑 매출 변화 같이 봐줘")

    assert result["decomposition"][0]["intent"] == "agent_loop"
    assert "deep_analysis_related_news" in _tool_names(result)
    assert "get_brand_metric" in _tool_names(result)
    assert "agent_calculation" in _tool_names(result)
    assert {"cache", "deep_analysis_events"}.issubset(set(result["sources"]))
    assert result["agent_loop_metrics"]["tool_selection_accuracy"] == 1.0
    assert "아토젯" in result["answer"]
    assert "매출 변화" in result["answer"]
    assert "데이터 없음" not in result["answer"]


def test_news_query_is_normalized_before_deep_analysis_filtering() -> None:
    planner = Stage3ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(name="search_news", arguments={"brand": "리바로", "query": "아토젯 이슈"}, reason="뉴스 이슈 확인"),
                )
            ),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    news = DeepAnalysisNewsTool(
        reader=StaticDeepAnalysisNewsReader(
            {
                "리바로": {
                    "data": {
                        "events": [
                            {
                                "date": "2026-04-12",
                                "title": "리바로 분기 매출 500억원 돌파",
                                "source": "약업신문",
                                "impact_score": 82,
                                "on_list": True,
                                "summary": "리바로 매출 흐름을 정리한 기사",
                                "body_full": "종근당 고지혈증 치료제 아토젯 261억원, 리바로는 500억원대 매출을 기록했다.",
                            }
                        ]
                    }
                }
            }
        )
    )
    metrics = _metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        news=news,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner, news=news),
    )

    result = agent.answer("리바로 뉴스에서 아토젯 이슈랑 매출 변화 같이 봐줘")

    news_call = next(call for call in result["tool_calls"] if call.get("tool") == "deep_analysis_related_news")
    assert news_call["applied_filters"] == {"text_contains": "아토젯"}
    assert news_call["render_data"]["items"][0]["match_excerpt"]
    trace_args = result["agent_trace"][0]["observations"][0]["arguments"]
    assert trace_args["query"] == "아토젯"


def test_issue_question_backfills_brand_metric_context_for_quantitative_link() -> None:
    planner = Stage3ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(name="search_news", arguments={"brand": "리바로", "query": "리바로"}, reason="최근 이슈 확인"),
                )
            ),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    metrics = _metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        news=_news_tool(),
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner, news=_news_tool()),
    )

    result = agent.answer("리바로 관련 최근 이슈 뭐 있어")

    metric_calls = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric" and call.get("render_data", {}).get("brand") == "리바로"
    ]
    assert metric_calls
    assert "리바로 지표 fact" in result["markdown_response"]["fact_md"]
    assert "매출" in result["markdown_response"]["fact_md"]


def test_comparison_brand_sales_change_is_completed_when_supported(tmp_path) -> None:
    metrics = _metrics_tool_with_atozet()
    resolver = _resolver_with_atozet(tmp_path)
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        news=_news_tool(),
        resolver=resolver,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=resolver, planner=HeuristicToolPlanner(), news=_news_tool()),
    )

    result = agent.answer("리바로 뉴스에서 아토젯 이슈랑 매출 변화 같이 봐줘")

    series_brands = [
        call["render_data"]["brand"]
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric" and call.get("render_data", {}).get("metric") == "series"
    ]
    delta_brands = [
        call["render_data"]["brand"]
        for call in result["tool_calls"]
        if call.get("tool") == "agent_calculation" and call.get("render_data", {}).get("metric") == "sales_delta"
    ]
    assert series_brands == ["리바로", "아토젯"]
    assert delta_brands == ["리바로", "아토젯"]
    assert result["markdown_response"]["fact_md"].count("아토젯 매출 시계열 fact") == 1


def test_sales_delta_uses_common_recent_periods_when_series_are_duplicated() -> None:
    """Given overlapping comparison-brand series, sales delta uses one value per common month."""

    calls = [
        {
            "tool": "get_brand_metric",
            "render_data": {
                "brand": "리바로",
                "brand_value_series_10pt": [
                    {"period": "2026-03", "value_krw": 8_711_248_139.54},
                    {"period": "2026-04", "value_krw": 8_493_234_217.11},
                ],
            },
        },
        {
            "tool": "get_brand_metric",
            "render_data": {
                "brand": "아토젯",
                "brand_value_series_10pt": [
                    {"period": "2026-03", "value_krw": 11_949_154_627.42},
                    {"period": "2026-04", "value_krw": 11_649_391_769.95},
                ],
            },
        },
        {
            "tool": "get_brand_metric",
            "render_data": {
                "brand": "아토젯",
                "brand_value_series_10pt": [
                    {"period": "2026-03", "value_krw": 11_949_154_627.42},
                    {"period": "2026-04", "value_krw": 11_649_391_769.95},
                ],
            },
        },
    ]

    deltas = _sales_delta_calls(calls)

    by_brand = {call["render_data"]["brand"]: call["render_data"] for call in deltas}
    assert by_brand["리바로"]["period"] == "2026-03→2026-04"
    assert by_brand["아토젯"]["period"] == "2026-03→2026-04"
    assert by_brand["아토젯"]["sales_delta_억원"] == -3.0


def test_comparison_brand_uses_market_member_segment_when_not_canonical() -> None:
    metrics = _metrics_tool_with_atozet_segment_only()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        news=_news_tool(),
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=HeuristicToolPlanner(), news=_news_tool()),
    )

    result = agent.answer("리바로 뉴스에서 아토젯 이슈랑 매출 변화 같이 봐줘")

    atozet_metrics = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric" and call.get("render_data", {}).get("brand") == "아토젯"
    ]
    unsupported_atozet = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "unsupported_metric" and call.get("render_data", {}).get("brand") == "아토젯"
    ]
    assert atozet_metrics
    assert not unsupported_atozet
    data = atozet_metrics[0]["render_data"]
    assert data["metric"] == "market_member_snapshot"
    assert data["ms_recent_pct"] == 5.1620
    assert data["sales_krw"] == 11_648_132_500.0
    assert data["rank"] == 4
    assert "아토젯 최신 시장 멤버 지표" in result["markdown_response"]["fact_md"]
    assert "아토젯 매출 변화는 현재 지원 브랜드 목록" not in result["markdown_response"]["fact_md"]


def test_news_sales_impact_backfills_news_metric_and_market_scope() -> None:
    metrics = _metrics_tool()
    news = _news_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        news=news,
        agent_loop=ToolUseAgent(
            metrics=metrics,
            resolver=BrandResolver(),
            planner=HeuristicToolPlanner(),
            news=news,
        ),
    )

    result = agent.answer("리바로 관련 뉴스가 최근 매출에 미친 영향")

    tools = {call["tool"] for call in result["tool_calls"]}
    assert {"deep_analysis_related_news", "get_brand_metric", "get_market_landscape"} <= tools
    assert "unsupported_metric" not in tools


def test_comparison_brand_uses_market_member_trend_when_not_canonical() -> None:
    metrics = _metrics_tool_with_atozet_trend_only()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        news=_news_tool(),
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=HeuristicToolPlanner(), news=_news_tool()),
    )

    result = agent.answer("리바로 뉴스에서 아토젯 이슈랑 매출 변화 같이 봐줘")

    atozet_series = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric"
        and call.get("render_data", {}).get("brand") == "아토젯"
        and call.get("render_data", {}).get("brand_value_series_10pt")
    ]
    unsupported_atozet = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "unsupported_metric" and call.get("render_data", {}).get("brand") == "아토젯"
    ]
    delta_brands = [
        call["render_data"]["brand"]
        for call in result["tool_calls"]
        if call.get("tool") == "agent_calculation" and call.get("render_data", {}).get("metric") == "sales_delta"
    ]
    assert atozet_series
    assert not unsupported_atozet
    assert "아토젯" in delta_brands
    assert "아토젯 매출 시계열 fact" in result["markdown_response"]["fact_md"]


def test_comparison_brand_metric_gap_is_explicit_when_unsupported() -> None:
    metrics = _metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        news=_news_tool(),
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=HeuristicToolPlanner(), news=_news_tool()),
    )

    result = agent.answer("리바로 뉴스에서 아토젯 이슈랑 매출 변화 같이 봐줘")

    unsupported = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "unsupported_metric" and call.get("render_data", {}).get("brand") == "아토젯"
    ]
    assert unsupported
    assert "지원 브랜드 목록" in unsupported[0]["summary_text"]
    assert "필수 답변 fact" in result["markdown_response"]["fact_md"]
    assert "데이터 미보유" in result["markdown_response"]["fact_md"]


def test_complex_hira_and_sales_question_uses_disease_and_metric_tools() -> None:
    planner = Stage3ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(name="get_disease_stats", arguments={"brand": "리바로하이"}, reason="질병 환자수 확인"),
                    ToolCallPlan(name="get_metric", arguments={"brand": "리바로하이", "measure": "sales", "period": "latest"}, reason="최근 매출 확인"),
                )
            ),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    metrics = _livalohigh_metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner, external=ExternalApiClient(mode="fixture")),
    )

    result = agent.answer("리바로하이 질병 환자수랑 최근 매출 한번에")

    assert result["decomposition"][0]["intent"] == "agent_loop"
    assert result["resolution"]["canonical_brand"] == "리바로하이"
    assert "get_disease_stats" in _tool_names(result)
    assert "get_brand_metric" in _tool_names(result)
    metric_calls = [call for call in result["tool_calls"] if call.get("tool") == "get_brand_metric"]
    assert metric_calls
    assert all(call.get("render_data", {}).get("brand") == "리바로하이" for call in metric_calls)
    assert all(call.get("render_data", {}).get("answer_scope") == "single_brand_focus" for call in metric_calls)
    assert {"cache", "hira_disease"}.issubset(set(result["sources"]))
    assert result["agent_loop_metrics"]["tool_selection_accuracy"] == 1.0
    assert "리바로하이 지표 fact" in result["markdown_response"]["fact_md"]
    assert "데이터 없음" not in result["answer"]
    assert "환자수" in result["answer"]
    assert "4010" in result["answer"]


def test_heuristic_patient_sales_question_requests_series_metric() -> None:
    decision = HeuristicToolPlanner().decide(
        "리바로하이 환자수+매출",
        (),
        (
            {"function": {"name": "get_disease_stats"}},
            {"function": {"name": "get_metric"}},
        ),
        allowed_brands=("리바로하이",),
        allowed_periods=("2026-04",),
    )

    metric_calls = [call for call in decision.tool_calls if call.name == "get_metric"]

    assert metric_calls
    assert metric_calls[0].arguments["measure"] == "series"


def test_heuristic_competitor_patent_preserves_question_for_competitor_scope() -> None:
    question = "[리바로] 경쟁 성분의 특허, 독점권은 어떠한가?"

    decision = HeuristicToolPlanner().decide(
        question,
        (),
        (
            {"function": {"name": "search_patent"}},
            {"function": {"name": "get_metric"}},
        ),
        allowed_brands=("리바로",),
        allowed_periods=("2026-04",),
    )

    patent_calls = [call for call in decision.tool_calls if call.name == "search_patent"]
    assert patent_calls
    assert patent_calls[0].arguments["query"] == question


def test_change_driver_question_gets_background_news_without_news_cue() -> None:
    question = "[리바로] 목표 시장에서의 향후 예상되는 시장 변화 요인이 있는가? - External: 타사 경쟁품 출시,  Market expansion, 보건 정책 변화(약가인하 등) - Internal: 자사 Line extension, 영업/채널 (타겟 Segment)"
    result = ChatAgent(external_mode="fixture").answer(question)
    revised = enforce_answer_contract(question, result["answer"], result.get("markdown_response"))

    assert "deep_analysis_related_news" in _tool_names(result)
    assert "## 변화 요인 결론" in revised
    assert "### External/Internal 결과표" in revised
    assert "뉴스 fact를 정성 근거로 분류해 연결합니다" not in revised
    assert "조건에 맞는 관련 뉴스 없음" not in result["markdown_response"]["fact_md"]


def test_direct_patent_question_surfaces_competitor_ingredient_context() -> None:
    result = ChatAgent(external_mode="fixture", query_layer=_query_layer()).answer("[리바로] 경쟁 성분의 특허, 독점권은 어떠한가?")

    fact_md = result["markdown_response"]["fact_md"]
    assert "search_patent" in _tool_names(result)
    assert "### 경쟁 성분 후보군 fact" in fact_md
    assert "### 경쟁 성분 특허 조회 커버리지 fact" in fact_md
    assert "현재 특허 DB에서 확인되는 항목만 표시" in fact_md


def test_patient_sales_question_backfills_series_when_planner_selected_latest_sales() -> None:
    planner = Stage3ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(name="get_disease_stats", arguments={"brand": "리바로"}, reason="환자수 확인"),
                    ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "sales", "period": "latest"}, reason="매출 확인"),
                )
            ),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    metrics = _metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner, external=ExternalApiClient(mode="fixture")),
    )

    result = agent.answer("리바로 환자수+매출")

    series_calls = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric"
        and len(call.get("render_data", {}).get("brand_value_series_10pt") or []) >= 2
    ]
    assert series_calls
    assert "매출 추이" in result["markdown_response"]["fact_md"]
    assert should_use_agent_loop("리바로하이 환자수+매출")


def test_sales_change_question_uses_series_and_renders_deterministic_delta() -> None:
    metrics = _metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=HeuristicToolPlanner()),
    )

    result = agent.answer("리바로 매출 변화 봐줘")

    assert result["decomposition"][0]["intent"] == "agent_loop"
    assert "get_brand_metric" in _tool_names(result)
    assert "agent_calculation" in _tool_names(result)
    assert any(call.get("render_data", {}).get("metric") == "series" for call in result["tool_calls"])
    assert any(call.get("render_data", {}).get("metric") == "sales_delta" for call in result["tool_calls"])
    assert "매출 변화" in result["answer"]
    assert "-2.18억원" in result["answer"]
    assert "-2.50%" in result["answer"]


def test_single_month_sales_call_does_not_block_full_series_completion() -> None:
    planner = Stage3ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "sales", "period": "2026-02"}, reason="2월 매출 확인"),
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

    result = agent.answer("리바로 2월 매출 하락이 시장 영향인지 브랜드 고유인지")

    full_series_calls = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric"
        and len(call.get("render_data", {}).get("brand_value_series_10pt") or []) >= 2
    ]
    assert full_series_calls
    assert full_series_calls[-1]["render_data"]["completion_reason"] == "sales_change_requires_series"


def test_fixture_agent_loop_metric_path_uses_period_grounding_display() -> None:
    result = ChatAgent().answer("리바로 같은 시장 작년 제일 큰 경쟁사")

    assert result["decomposition"][0]["intent"] == "agent_loop"
    assert "unsupported_metric" not in _tool_names(result)
    assert "get_brand_metric" in _tool_names(result)
    assert "NameError" not in result["answer"]


def test_clinical_and_patent_facade_tools_return_policy_scoped_facts() -> None:
    planner = Stage3ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(name="search_clinical", arguments={"brand": "리바로"}, reason="임상 확인"),
                    ToolCallPlan(name="search_patent", arguments={"brand": "리바로"}, reason="특허 확인"),
                )
            ),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    metrics = _metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner, external=ExternalApiClient(mode="fixture")),
    )

    result = agent.answer("리바로 임상하고 특허를 같이 확인해줘")

    assert result["decomposition"][0]["intent"] == "agent_loop"
    assert {"search_clinical", "search_patent"}.issubset(set(_tool_names(result)))
    assert "external_api" in result["sources"]
    assert result["agent_loop_metrics"]["tool_selection_accuracy"] == 1.0


def test_external_intent_schema_filter_does_not_add_metric_fallback() -> None:
    schemas = (
        {"type": "function", "function": {"name": "search_patent", "description": "", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "search_clinical", "description": "", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "get_procedure_stats", "description": "", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "web_search", "description": "", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "get_metric", "description": "", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "get_market_scope", "description": "", "parameters": {"type": "object"}}},
    )

    patent_selected = select_candidate_tools("[리바로] 경쟁 성분의 특허, 독점권은 어떠한가?", schemas, ())
    patent_names = [item["function"]["name"] for item in patent_selected]

    assert "search_patent" in patent_names
    assert "get_metric" not in patent_names

    mixed_selected = select_candidate_tools("[리바로] 특허와 매출을 같이 알려줘", schemas, ())
    mixed_names = [item["function"]["name"] for item in mixed_selected]

    assert "search_patent" in mixed_names
    assert "get_metric" in mixed_names

    procedure_selected = select_candidate_tools("[리바로] MM302 진료행위 성별 입원외래 통계 알려줘", schemas, ())
    procedure_names = [item["function"]["name"] for item in procedure_selected]

    assert "get_procedure_stats" in procedure_names
    assert "get_metric" not in procedure_names

    web_selected = select_candidate_tools("[리바로] 경쟁제품의 최근 상기되는 디테일링 주요 내용은 무엇인가?", schemas, ())
    web_names = [item["function"]["name"] for item in web_selected]

    assert "web_search" in web_names
    assert "search_patent" not in web_names


def test_agent_loop_routing_includes_hira_procedure_and_web_search_intents() -> None:
    assert should_use_agent_loop("[리바로] MM302 진료행위 성별 입원외래 통계 알려줘")
    assert should_use_agent_loop("[리바로] 경쟁제품의 최근 상기되는 디테일링 주요 내용은 무엇인가?")


def test_genos_planner_uses_deterministic_external_tools_before_llm() -> None:
    class FallbackBomb:
        def decide(self, *_args, **_kwargs):
            raise AssertionError("fallback should not be called for explicit external intents")

    decision = GenosToolPlanner(fallback=FallbackBomb(), token=None).decide(
        "[리바로] 경쟁제품의 최근 상기되는 디테일링 주요 내용은 무엇인가?",
        (),
        (),
        ("리바로",),
        ("2026-04",),
    )

    assert [call.name for call in decision.tool_calls] == ["web_search"]

    procedure_decision = GenosToolPlanner(fallback=FallbackBomb(), token=None).decide(
        "[리바로] MM302 진료행위 성별 입원외래 통계 알려줘",
        (),
        (),
        ("리바로",),
        ("2026-04",),
    )

    assert [call.name for call in procedure_decision.tool_calls] == ["get_procedure_stats"]


def test_genos_planner_routes_explicit_web_search_words_before_llm() -> None:
    class FallbackBomb:
        def decide(self, *_args, **_kwargs):
            raise AssertionError("fallback should not be called for explicit web search intent")

    planner = GenosToolPlanner(fallback=FallbackBomb(), token=None)

    for question in (
        "리바로 시장동향을 웹 검색 결과(미검증) 섹션으로 URL과 snippet만 분리해서 보여줘",
        "리바로 경쟁제품 최근 동향 검색해줘",
    ):
        decision = planner.decide(question, (), (), ("리바로",), ("2026-04",))

        assert [call.name for call in decision.tool_calls] == ["web_search"]


def test_web_search_query_adds_pharma_brand_and_molecule_context() -> None:
    @dataclass(frozen=True, slots=True)
    class Resolution:
        canonical_brand: str
        molecule_en: tuple[str, ...]
        is_combo: bool = False

    query = _web_search_query("경쟁제품 최근 동향 검색해줘", Resolution("리바로", ("pitavastatin",)))

    assert query == "리바로 pitavastatin 제약 의약품 경쟁제품 최근 동향 검색해줘"


def test_hira_procedure_and_web_search_fixture_tools_render_payloads() -> None:
    planner = Stage3ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(
                        name="get_procedure_stats",
                        arguments={"brand": "리바로", "query": "MM302 진료행위 성별 입원외래 통계"},
                        reason="진료행위 통계 확인",
                    ),
                    ToolCallPlan(
                        name="web_search",
                        arguments={"brand": "리바로", "query": "리바로 경쟁제품 디테일링 주요 내용"},
                        reason="외부 웹 검색 확인",
                    ),
                )
            ),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    metrics = _metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner, external=ExternalApiClient(mode="fixture")),
    )

    result = agent.answer("리바로 MM302 진료행위와 경쟁제품 디테일링을 같이 확인해줘")

    tool_names = _tool_names(result)
    assert "get_procedure_stats" in tool_names
    assert "web_search" in tool_names
    assert {"hira_procedure", "web_search"}.issubset(set(result["sources"]))
    assert "HIRA 진료행위통계" in result["markdown_response"]["data_md"]
    assert "MM302" in result["markdown_response"]["data_md"]
    assert "웹 검색 결과(미검증)" in result["markdown_response"]["data_md"]
    assert "https://example.com/livalo-detailing" in result["markdown_response"]["data_md"]


def test_mfds_clinical_broad_rows_are_filtered_from_evidence() -> None:
    result = clinical_call(_ExternalResolution(canonical_brand="리바로", molecule_en=("pitavastatin",)), _ClinicalBroadExternal())
    calls = result["render_data"]["calls"]
    mfds_call = next(call for call in calls if call["tool"] == "mfds_clinical_trial_kr")
    clinicaltrials_call = next(call for call in calls if call["tool"] == "clinicaltrials_v2_search")

    assert clinicaltrials_call["status"] == "live"
    assert "NCT05537948" in str(clinicaltrials_call["render_data"])
    assert mfds_call["status"] == "no_data"
    assert mfds_call["render_data"]["items"] == []
    assert mfds_call["render_data"]["filtered_from_count"] == 2
    assert "CJ-20001" not in str(result)


def test_mfds_drug_info_facade_relays_permission_detail_without_easy_drug() -> None:
    planner = Stage3ScriptedPlanner(
        (
            AgentDecision(tool_calls=(ToolCallPlan(name="search_drug_info", arguments={"brand": "리바로"}, reason="식약처 허가정보 확인"),)),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    metrics = _metrics_tool()
    external = _PermissionOnlyExternal()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner, external=external),
    )

    result = agent.answer("리바로 식약처 허가정보 알려줘")

    assert "search_drug_info" in _tool_names(result)
    call = next(call for call in result["tool_calls"] if call.get("tool") == "search_drug_info")
    nested_tools = [item["tool"] for item in call["render_data"]["calls"]]
    assert nested_tools == ["mfds_permission_search", "mfds_permission_detail"]
    assert "mfds_easy_drug" not in nested_tools
    assert "MFDS 허가정보" in result["markdown_response"]["data_md"]
    assert "리바로정1밀리그램" in result["markdown_response"]["data_md"]
    assert "전문의약품" in result["markdown_response"]["data_md"]


def test_mfds_drug_info_facade_fails_closed_for_empty_permission_search() -> None:
    planner = Stage3ScriptedPlanner(
        (
            AgentDecision(tool_calls=(ToolCallPlan(name="search_drug_info", arguments={"brand": "리바로"}, reason="식약처 허가정보 확인"),)),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    metrics = _metrics_tool()
    external = _EmptyPermissionExternal()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=planner, external=external),
    )

    result = agent.answer("리바로 식약처 허가정보 알려줘")

    call = next(call for call in result["tool_calls"] if call.get("tool") == "search_drug_info")
    nested_tools = [item["tool"] for item in call["render_data"]["calls"]]
    assert nested_tools == ["mfds_permission_search", "mfds_permission_search"]
    assert call["render_data"]["status"] == "partial"
    assert "조회 결과 없음" in result["answer"] or "근거 생성 안 함" in result["answer"] or "브랜드 일치 결과" in result["markdown_response"]["fact_md"]
    assert "mfds_easy_drug" not in nested_tools


def test_heuristic_planner_routes_domestic_permission_question_to_drug_info() -> None:
    planner = HeuristicToolPlanner()

    decision = planner.decide(
        "리바로 식약처 허가정보 알려줘",
        (),
        (
            {
                "function": {
                    "name": "search_drug_info",
                    "parameters": {"properties": {"brand": {"enum": ["리바로"]}}},
                }
            },
        ),
        ("리바로",),
        (),
    )

    assert decision.tool_calls
    assert decision.tool_calls[0].name == "search_drug_info"


class _PermissionOnlyExternal(ExternalApiClient):
    def __init__(self) -> None:
        super().__init__(mode="fixture")

    def mfds_easy_drug(self, item_seq: str) -> ExternalCall:
        raise AssertionError(f"mfds_easy_drug must not be called by search_drug_info: {item_seq}")


class _EmptyPermissionExternal(_PermissionOnlyExternal):
    def mfds_permission_search(self, brand: str) -> ExternalCall:
        return ExternalCall(
            tool="mfds_permission_search",
            source="external_api",
            status="live",
            summary_text=f"MFDS 품목 검색에서 {brand} 결과 없음",
            render_data={"resultCode": "00", "totalCount": "0", "items": [], "request": {"brand": brand}},
        )

    def mfds_permission_detail(self, item_seq: str) -> ExternalCall:
        raise AssertionError(f"detail lookup must not run without a matching ITEM_SEQ: {item_seq}")


def test_judgment_metric_question_gets_background_news_context_without_news_cue() -> None:
    metrics = _metrics_tool()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        news=_news_tool(),
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=HeuristicToolPlanner(), news=_news_tool()),
    )

    result = agent.answer("리바로 경쟁 구도 변화는 어때")

    news_calls = [call for call in result["tool_calls"] if call.get("tool") == "deep_analysis_related_news"]
    assert news_calls
    assert news_calls[0]["render_data"]["context_role"] == "background_insight"
    assert "인사이트 근거 fact - 뉴스/이슈" in result["markdown_response"]["fact_md"]
    assert "아토젯 약가 이슈 이후 리바로 처방 동향" in result["markdown_response"]["fact_md"]


def test_judgment_background_news_uses_market_structure_relevance_brands() -> None:
    news = DeepAnalysisNewsTool(
        reader=StaticDeepAnalysisNewsReader(
            {
                "리바로": {
                    "data": {
                        "events": [
                            {
                                "date": "2026-06-11",
                                "title": "리바로 단독 generic 스타틴 기사",
                                "source": "약사공론",
                                "url": "https://news.example/livalo-generic",
                                "impact_score": 99,
                                "on_list": True,
                                "summary": "리바로 anchor만 걸린 generic 배경 기사",
                            }
                        ]
                    }
                },
                "리피토": {
                    "data": {
                        "events": [
                            {
                                "date": "2026-06-10",
                                "title": "리피토 점유율 하락 기사",
                                "source": "데일리팜",
                                "url": "https://news.example/lipitor-share",
                                "impact_score": 92,
                                "on_list": True,
                                "summary": "경쟁 브랜드 점유율 변화 기사",
                            }
                        ]
                    }
                },
                "아토젯": {
                    "data": {
                        "events": [
                            {
                                "date": "2026-06-09",
                                "title": "아토젯 경쟁 구도 기사",
                                "source": "히트뉴스",
                                "url": "https://news.example/atozet-competition",
                                "impact_score": 91,
                                "on_list": True,
                                "summary": "시장 경쟁 구도 기사",
                            }
                        ]
                    }
                },
            }
        )
    )
    metrics = _metrics_tool_with_atozet_trend_only()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        news=news,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=HeuristicToolPlanner(), news=news),
    )

    result = agent.answer("리바로 경쟁 구도 변화는 어때")

    news_call = next(call for call in result["tool_calls"] if call.get("tool") == "deep_analysis_related_news")
    relevance_filter = news_call["applied_filters"]["relevance_brands"]
    assert " OR " in relevance_filter
    assert "리피토" in relevance_filter
    assert "아토젯" in relevance_filter
    titles = [item["title"] for item in news_call["render_data"]["items"]]
    assert "리바로 단독 generic 스타틴 기사" not in titles
    assert titles == ["리피토 점유율 하락 기사", "아토젯 경쟁 구도 기사"]


def test_quantitative_questions_suppress_background_news_context() -> None:
    metrics = _metrics_tool()
    agent = ChatAgent(router=BQRouter(), metrics=metrics, news=_news_tool())

    for question in (
        "리바로 매출 알려줘",
        "리바로 채널",
        "리바로젯 시장 규모",
        "리바로 점유율 순위",
        "리바로 최근 매출 추이 어때",
    ):
        result = agent.answer(question)

        news_calls = [call for call in result["tool_calls"] if call.get("tool") == "deep_analysis_related_news"]
        assert not news_calls, question
        assert "인사이트 근거 fact - 뉴스/이슈" not in result["markdown_response"]["fact_md"]


def test_simple_share_rank_question_uses_agent_loop_for_market_scope_consistency() -> None:
    assert should_use_agent_loop("리바로 점유율 순위")
    assert should_use_agent_loop("리바로 순위 알려줘")


def test_single_brand_sales_trend_suppresses_top_brand_context() -> None:
    metrics = _metrics_tool_with_atozet_trend_only()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=HeuristicToolPlanner()),
    )

    result = agent.answer("리바로 최근 매출 추이 어때")
    fact_md = result["markdown_response"]["fact_md"]

    series_calls = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric" and call.get("render_data", {}).get("metric") == "series"
    ]
    assert series_calls
    assert all(call["render_data"].get("answer_scope") == "single_brand_trend" for call in series_calls)
    assert "리바로 매출 시계열 fact" in fact_md
    assert "상위 브랜드 점유율 추이 fact" not in fact_md
    assert "| 상위 브랜드 추이 |" not in fact_md


def test_competitive_landscape_adds_deterministic_agent2_insight_signals() -> None:
    metrics = _metrics_tool_with_atozet_trend_only()
    agent = ChatAgent(
        router=BQRouter(),
        metrics=metrics,
        agent_loop=ToolUseAgent(metrics=metrics, resolver=BrandResolver(), planner=HeuristicToolPlanner()),
    )

    result = agent.answer("리바로 경쟁 구도 변화는 어때")
    insight_calls = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "agent_calculation" and call.get("render_data", {}).get("metric") == "competitive_insight_signals"
    ]

    assert insight_calls
    signals = insight_calls[0]["render_data"]["signals"]
    assert all(signal.get("period_from") for signal in signals)
    assert all(signal.get("period_to") == "2026-04" for signal in signals)
    assert all(signal.get("comparison_basis") == "analysis_period" for signal in signals)
    assert insight_calls[0]["render_data"].get("surface_policy", {}).get("gain_loss_ratio_pct") == "internal_only"
    period = insight_calls[0]["render_data"]["period"]
    assert "인사이트 계산" in result["markdown_response"]["fact_md"]
    assert "share-of-growth" in result["markdown_response"]["fact_md"]
    assert "93.62%" not in result["markdown_response"]["fact_md"]
    assert f"{period} 점유율 변화" in result["markdown_response"]["fact_md"]
