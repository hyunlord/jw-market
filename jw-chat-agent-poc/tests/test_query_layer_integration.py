from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from jw_chat_agent_poc.agent_loop import should_use_agent_loop
from jw_chat_agent_poc.agent_loop.loop import ToolUseAgent
from jw_chat_agent_poc.agent_loop.models import AgentDecision, ToolCallPlan
from jw_chat_agent_poc.agent_loop.population_specs import strict_query_plan
from jw_chat_agent_poc.agent_loop.schemas import tool_schemas
from jw_chat_agent_poc.agent_loop.tools import AgentToolFacade
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.metrics.cache_live import StaticCausePayloadReader, StaticMetricsCacheReader
from jw_chat_agent_poc.tools.query_layer import MartRecord, StaticStrategicMartReader, StrategicQueryLayer

from test_metrics_cache import BRAND_CARDS, CACHE_BRANDS


@dataclass(slots=True)
class ScriptedPlanner:
    decisions: tuple[AgentDecision, ...]
    index: int = 0
    schema_history: list[tuple[dict[str, Any], ...]] | None = None

    def decide(
        self,
        _question: str,
        _observations,
        schemas: tuple[dict[str, Any], ...],
        allowed_brands: tuple[str, ...] = (),
        allowed_periods: tuple[str, ...] = (),
    ) -> AgentDecision:
        if self.schema_history is None:
            self.schema_history = []
        self.schema_history.append(schemas)
        decision = self.decisions[min(self.index, len(self.decisions) - 1)]
        self.index += 1
        return decision


def test_query_schema_injects_market_catalog_enums() -> None:
    """Given a strategic market, tool schemas expose only catalog-valid identifiers."""

    layer = _query_layer()
    catalog = layer.catalog_for_brand("리바로")

    schemas = tool_schemas(("리바로",), ("latest", "2026-04"), catalog)

    query_schema = next(schema for schema in schemas if schema["function"]["name"] == "query")
    dimension_enum = query_schema["function"]["parameters"]["properties"]["spec"]["properties"]["dimensions"]["items"]["enum"]
    assert "product" in dimension_enum
    assert "company" in dimension_enum
    assert "nhi_type" not in dimension_enum


def test_facade_prefers_query_layer_for_strategic_metric() -> None:
    """Given the query layer is available, get_metric returns mart-derived facts."""

    facade = AgentToolFacade(
        metrics=_metrics_tool(),
        resolver=BrandResolver(),
        allowed_brands=("리바로",),
        query_layer=_query_layer(),
    )

    execution = facade.execute("get_metric", {"brand": "리바로", "measure": "series", "period": "latest"})

    data = execution.call["render_data"]
    assert execution.call["source"] == "UBIST"
    assert data["brand"] == "리바로"
    assert data["period"] == "2026-04"
    assert data["sales_krw"] == 9_000_000_000.0
    assert data["query_result_id"].startswith("qr_")
    assert {row["brand"] for row in data["level_top5_trend_series"]} >= {"아토젯", "리바로"}
    livaro = next(row for row in data["level_top5_trend_series"] if row["brand"] == "리바로")
    assert livaro["from_period"] == livaro["series"][0]["period"]
    assert livaro["from_ms_pct"] == livaro["series"][0]["ms_pct"]
    assert livaro["to_period"] == livaro["series"][-1]["period"]
    assert livaro["to_ms_pct"] == livaro["series"][-1]["ms_pct"]


def test_market_member_metric_reads_comparison_brand_series() -> None:
    """Given a market-member brand, query layer can return comparison series without resolver support."""

    call = _query_layer().market_member_metric("리바로", "아토젯")

    data = call["render_data"]
    assert call["source"] == "UBIST"
    assert data["brand"] == "아토젯"
    assert data["metric"] == "market_member_series"
    assert data["ms_recent_pct"] == pytest.approx(13_000_000_000.0 / 81_800_000_000.0 * 100)
    assert len(data["brand_value_series_10pt"]) == 4


def test_query_spec_rejects_absent_market_dimension() -> None:
    """Given an absent dimension, query(spec) fails schema validation."""

    with pytest.raises(ValueError, match="dimensions unknown"):
        _query_layer().query({"market": "ml_006", "source": "ubist", "dimensions": ["nhi_type"], "metrics": ["sales"]}, fallback_brand="리바로")


def test_query_spec_applies_channel_filter_to_molecule_population() -> None:
    """Given a channel filter, query(spec) returns molecule shares from that channel population."""

    call = _query_layer().query(
        {
            "market": "ml_006",
            "source": "ubist",
            "dimensions": ["molecule"],
            "group_by": ["molecule"],
            "metrics": ["share"],
            "filters": {"channel": "의원"},
        },
        fallback_brand="리바로",
    )

    data = call["render_data"]
    rows = {row["name"]: row for row in data["level_segments"]}
    assert data["applied_filters"] == {"channel": "의원"}
    assert rows["피타바스타틴"]["ms_recent_pct"] == pytest.approx(4_500_000_000 / 22_700_000_000 * 100)
    assert rows["아토젯성분"]["ms_recent_pct"] == pytest.approx(3_250_000_000 / 22_700_000_000 * 100)


def test_query_spec_groups_channel_and_specialty_nested_populations() -> None:
    """Given nested mart dimensions, query(spec) groups their latest-period values."""

    channel_call = _query_layer().query(
        {"market": "ml_006", "source": "ubist", "dimensions": ["channel"], "group_by": ["channel"], "metrics": ["share"]},
        fallback_brand="리바로",
    )
    specialty_call = _query_layer().query(
        {"market": "ml_006", "source": "ubist", "dimensions": ["specialty"], "group_by": ["specialty"], "metrics": ["sales"]},
        fallback_brand="리바로",
    )

    channels = {row["name"]: row for row in channel_call["render_data"]["level_segments"]}
    specialties = {row["name"]: row for row in specialty_call["render_data"]["level_segments"]}
    assert channels["의원"]["value"] == pytest.approx(22_700_000_000)
    assert channels["종병"]["value"] == pytest.approx(31_820_000_000)
    assert specialties["순환기"]["value"] == pytest.approx(49_080_000_000)


def test_query_spec_builds_dimension_trend_for_period_grouping() -> None:
    """Given period grouping, query(spec) exposes fact-backed series for top dimension groups."""

    call = _query_layer().query(
        {
            "market": "ml_006",
            "source": "ubist",
            "dimensions": ["dosage_form"],
            "group_by": ["dosage_form", "period"],
            "metrics": ["sales"],
            "derive": ["trend"],
            "filters": {"periods": "3"},
            "limit": 5,
        },
        fallback_brand="리바로",
    )

    data = call["render_data"]
    trend = data["level_top5_trend_series"]
    assert data["level"] == "dosage_form"
    assert {row["brand"] for row in trend} >= {"정제", "복합정"}
    first = next(row for row in trend if row["brand"] == "정제")
    assert [point["period"] for point in first["series"]] == ["2026-02", "2026-03", "2026-04"]
    assert first["from_ms_pct"] == first["series"][0]["ms_pct"]
    assert first["to_ms_pct"] == first["series"][-1]["ms_pct"]


def test_query_spec_calculates_yoy_growth_and_average_share() -> None:
    """Given derive specs, query(spec) returns deterministic YoY and average facts."""

    yoy_call = _query_layer().query(
        {
            "market": "ml_006",
            "source": "ubist",
            "dimensions": ["product"],
            "group_by": ["product"],
            "metrics": ["growth"],
            "derive": ["yoy"],
            "filters": {"brand": "리바로"},
        },
        fallback_brand="리바로",
    )
    avg_call = _query_layer().query(
        {
            "market": "ml_006",
            "source": "ubist",
            "dimensions": ["product"],
            "group_by": ["product"],
            "metrics": ["share"],
            "derive": ["average"],
            "filters": {"brand": "리바로", "periods": "6"},
        },
        fallback_brand="리바로",
    )

    yoy_data = yoy_call["render_data"]
    avg_data = avg_call["render_data"]
    assert yoy_data["brand"] == "리바로"
    assert yoy_data["metric"] == "yoy_growth"
    assert yoy_data["growth_pct"] == pytest.approx((9_000_000_000 / 7_000_000_000 - 1) * 100, abs=0.0001)
    assert avg_data["metric"] == "average_share"
    assert avg_data["avg_ms_pct"] == pytest.approx(sum(point["ms_pct"] for point in avg_data["brand_value_series_10pt"]) / 4, abs=0.0001)


def test_completion_uses_query_layer_for_unsupported_comparison_brand() -> None:
    """Given 아토젯 is not in resolver catalog, completion recovers it from mart series."""

    planner = ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "series", "period": "latest"}, reason="anchor series"),)
            ),
            AgentDecision(final_answer="done"),
        )
    )
    agent = ToolUseAgent(metrics=_metrics_tool(), resolver=BrandResolver(), planner=planner, query_layer=_query_layer())

    result = agent.answer("리바로 뉴스에서 아토젯 이슈랑 매출 변화 같이 봐줘")

    comparison_calls = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric" and call.get("render_data", {}).get("brand") == "아토젯"
    ]
    assert comparison_calls
    assert comparison_calls[0]["source"] == "UBIST"
    assert comparison_calls[0]["render_data"]["metric"] == "market_member_series"


def test_share_comparison_completion_uses_query_layer_for_market_member() -> None:
    """Given a share trend comparison, unsupported comparison terms are completed from mart."""

    planner = ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "series", "period": "latest"}, reason="anchor series"),)
            ),
            AgentDecision(final_answer="done"),
        )
    )
    agent = ToolUseAgent(metrics=_metrics_tool(), resolver=BrandResolver(), planner=planner, query_layer=_query_layer())

    result = agent.answer("리바로와 아토젯의 점유율 변화 비교")

    brands = {
        call.get("render_data", {}).get("brand")
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric"
    }
    assert {"리바로", "아토젯"}.issubset(brands)


def test_threat_question_adds_pair_trend_comparison_fact() -> None:
    """Given a threat question, agent computes a two-brand trend comparison fact."""

    planner = ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "series", "period": "latest"}, reason="anchor series"),)
            ),
            AgentDecision(final_answer="done"),
        )
    )
    agent = ToolUseAgent(metrics=_metrics_tool(), resolver=BrandResolver(), planner=planner, query_layer=_query_layer())

    result = agent.answer("아토젯이 리바로를 위협하고 있어?")

    calculations = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "agent_calculation" and call.get("render_data", {}).get("metric") == "brand_trend_comparison"
    ]
    assert calculations
    comparison = calculations[0]["render_data"]
    assert comparison["brand"] == "리바로"
    assert comparison["comparison_brand"] == "아토젯"
    assert comparison["brand_share_delta_pctp"] == pytest.approx(
        result["tool_calls"][0]["render_data"]["level_top5_trend_series"][-1]["share_delta_pctp"]
    )
    assert "브랜드 추세 비교" in result["markdown_response"]["fact_md"]
    assert "아토젯" in result["markdown_response"]["fact_md"]


def test_population_question_forces_query_spec_instead_of_generic_metric() -> None:
    """Given a channel dimension question, agent adds a strict query(spec) population call."""

    planner = ScriptedPlanner(
        (
            AgentDecision(tool_calls=(ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "sales", "period": "latest"}, reason="generic metric"),)),
            AgentDecision(final_answer="done"),
        )
    )
    agent = ToolUseAgent(metrics=_metrics_tool(), resolver=BrandResolver(), planner=planner, query_layer=_query_layer())

    result = agent.answer("리바로 의원 채널에서 성분별 점유율")

    query_calls = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric" and call.get("render_data", {}).get("metric") == "query_spec"
    ]
    assert query_calls
    data = query_calls[0]["render_data"]
    assert data["level"] == "Molecule"
    assert data["applied_filters"] == {"channel": "의원"}


@pytest.mark.parametrize(
    "question",
    [
        "리바로 채널별로 보여줘",
        "리바로 채널",
        "리바로 어느 채널에서 잘 팔려?",
        "리바로 의원/병원별 실적",
        "리바로 채널별 매출",
        "리바로 유통 채널",
        "리바로 채널 구성",
        "리바로 채널 분포",
        "리바로 채널 mix",
    ],
)
def test_generic_channel_question_maps_to_channel_distribution_query(question: str) -> None:
    planner = ScriptedPlanner((AgentDecision(final_answer="done"),))
    agent = ToolUseAgent(metrics=_metrics_tool(), resolver=BrandResolver(), planner=planner, query_layer=_query_layer())

    result = agent.answer(question)

    query_calls = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric" and call.get("render_data", {}).get("metric") == "query_spec"
    ]
    assert query_calls
    data = query_calls[0]["render_data"]
    rows = {row["name"]: row for row in data["level_segments"]}
    assert data["level"] == "channel"
    assert data["applied_filters"] == {"brand": "리바로"}
    assert rows["의원"]["value"] == pytest.approx(4_500_000_000.0)
    assert rows["종병"]["value"] == pytest.approx(2_700_000_000.0)


def test_channel_share_question_keeps_share_metric() -> None:
    plan = strict_query_plan("리바로와 아토젯 채널별 점유율", "리바로")

    assert plan is not None
    assert [spec["metrics"] for spec in plan.specs] == [["share"], ["share"]]
    assert [spec["filters"] for spec in plan.specs] == [{"brand": "리바로"}, {"brand": "아토젯"}]


@pytest.mark.parametrize(
    "question",
    [
        "리바로 채널별로 보여줘",
        "리바로 채널",
        "리바로 의원/병원별 실적",
        "리바로 유통 채널",
    ],
)
def test_channel_paraphrases_enter_agent_loop(question: str) -> None:
    assert should_use_agent_loop(question)


@pytest.mark.parametrize(
    "question",
    [
        "리바로 채널 파트너",
        "리바로 유튜브 채널",
    ],
)
def test_non_analytic_channel_phrases_do_not_enter_agent_loop(question: str) -> None:
    assert not should_use_agent_loop(question)


def test_absent_dimension_question_blocks_generic_cache_fallback() -> None:
    """Given a missing dimension request, agent returns unsupported only for that dimension intent."""

    planner = ScriptedPlanner(
        (
            AgentDecision(tool_calls=(ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "series", "period": "latest"}, reason="generic trend"),)),
            AgentDecision(final_answer="done"),
        )
    )
    agent = ToolUseAgent(metrics=_metrics_tool(), resolver=BrandResolver(), planner=planner, query_layer=_query_layer())

    result = agent.answer("리바로 급여/비급여 매출 구성과 추이")

    assert [call.get("tool") for call in result["tool_calls"]] == ["unsupported_metric"]
    assert "nhi_type" in result["tool_calls"][0]["render_data"]["message"]


def test_top_brand_questions_enter_agent_loop() -> None:
    """Given a top-brands question, routing should use query-capable agent loop."""

    assert should_use_agent_loop("리바로 시장에서 상위 브랜드 뭐 있어")


@pytest.mark.parametrize(
    "question",
    [
        "리바로와 아토젯의 채널별 점유율 차이",
        "리바로 시장 오리지널 vs 제네릭 비중",
        "리바로 제형별 매출 추이(최근 1년)",
        "리바로 시장에서 급매출 회사 top3와 그 성분",
        "리바로 급여/비급여 매출 구성과 추이",
        "리바로의 지난 6개월 평균 점유율은?",
    ],
)
def test_population_sensitive_questions_enter_agent_loop(question: str) -> None:
    """Given dimension or aggregation intents, routing must not use generic cache metrics."""

    assert should_use_agent_loop(question)


def test_simple_sales_question_stays_single_shot() -> None:
    """Given a simple metric request, fallback blocking should not break single-shot metrics."""

    assert not should_use_agent_loop("리바로 매출")


def _query_layer() -> StrategicQueryLayer:
    return StrategicQueryLayer(reader=StaticStrategicMartReader(_records()))


def _metrics_tool() -> MetricsTool:
    return MetricsTool(
        mode="cache",
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS),
        cause_reader=StaticCausePayloadReader({}),
    )


def _records() -> tuple[MartRecord, ...]:
    periods = ("2025-04", "2026-02", "2026-03", "2026-04")
    values = {
        "로수젯": (19_000_000_000.0, 20_000_000_000.0, 21_000_000_000.0, 22_000_000_000.0),
        "리피토": (14_000_000_000.0, 15_000_000_000.0, 14_000_000_000.0, 14_500_000_000.0),
        "아토젯": (10_000_000_000.0, 11_000_000_000.0, 12_000_000_000.0, 13_000_000_000.0),
        "리바로젯": (11_500_000_000.0, 12_000_000_000.0, 12_500_000_000.0, 12_800_000_000.0),
        "로수바미브": (9_500_000_000.0, 10_000_000_000.0, 10_200_000_000.0, 10_500_000_000.0),
        "리바로": (7_000_000_000.0, 8_000_000_000.0, 8_500_000_000.0, 9_000_000_000.0),
    }
    totals = [sum(series[index] for series in values.values()) for index in range(len(periods))]
    return tuple(_record(brand, series, periods, totals) for brand, series in values.items())


def _record(brand: str, values: tuple[float, ...], periods: tuple[str, ...], totals: list[float]) -> MartRecord:
    history = {
        period: {"raw_value": values[index], "ms": values[index] / totals[index] * 100}
        for index, period in enumerate(periods)
    }
    channel_data = {
        "의원": _scaled_history(history, 0.50 if brand == "리바로" else 0.25),
        "종병": _scaled_history(history, 0.30 if brand == "리바로" else 0.40),
    }
    specialty_data = {
        "순환기": _scaled_history(history, 0.60),
        "내분비": _scaled_history(history, 0.25),
    }
    company = "JW중외제약" if brand in {"리바로", "리바로젯"} else "경쟁제약"
    molecule = "피타바스타틴" if brand == "리바로" else f"{brand}성분"
    dosage_form = "정제" if brand in {"리바로", "리피토"} else "복합정"
    ox_gx = "Original" if brand in {"리바로", "리피토"} else "Generic"
    return MartRecord(
        ml_id="ml_006",
        brand_name=brand,
        source="ubist",
        measure="sales",
        metric_history=history,
        channel_data=channel_data,
        specialty_data=specialty_data,
        dimension_data={"company": {company: history}, "ox_gx": {ox_gx: history}, "dosage_form": {dosage_form: history}},
        by_dimension={"company": company, "molecule": molecule, "ox_gx": ox_gx, "dosage_form": dosage_form, "class": dosage_form},
    )


def _scaled_history(history: dict[str, dict[str, float]], ratio: float) -> dict[str, dict[str, float]]:
    return {period: {"raw_value": row["raw_value"] * ratio} for period, row in history.items()}
