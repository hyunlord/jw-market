from __future__ import annotations

from datetime import datetime
from threading import Lock
from time import perf_counter, sleep

import pytest

from jw_chat_agent_poc import ChatAgent
from jw_chat_agent_poc.agent_loop.loop import ToolUseAgent
from jw_chat_agent_poc.agent_loop.planner import GenosToolPlanner
from jw_chat_agent_poc.agent_loop.tools import AgentToolFacade
from jw_chat_agent_poc.orchestrator.insight_acceptance import verify_insight_answer
from jw_chat_agent_poc.orchestrator.market_insights import forbidden_claims
from jw_chat_agent_poc.orchestrator.answer_facts import answer_fact_markdown
from jw_chat_agent_poc.orchestrator.provenance import evidence_from_calls
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.service.answer_safety import fallback_fact_answer
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.query_layer import MartRecord, StaticStrategicMartReader, StrategicQueryLayer


class _PlannerBomb:
    def decide(self, *_args, **_kwargs):
        raise AssertionError("explicit single-period sales must bypass the injected LLM planner")


def test_default_agent_executes_structured_plan_without_llm() -> None:
    layer = _layer()
    agent = ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=BrandResolver(mode="fixture"),
        query_layer=layer,
    )

    result = agent.answer("리바로 최근 시장점유율 추이")

    metrics = result["agent_loop_metrics"]
    assert metrics["deterministic_plan_hit"] is True
    assert metrics["deterministic_plan_kind"] == "brand_share"
    assert metrics["llm_plan_calls"] == 0
    assert set(metrics["selected_tools"]) >= {
        "get_brand_share",
        "get_brand_sales",
        "get_brand_series",
        "get_top_brands",
    }


def test_competitor_growth_table_fetches_yoy_for_each_top_brand() -> None:
    periods = tuple(f"{year}-{month:02d}" for year in (2025, 2026) for month in range(1, 7))
    values = {
        "로수젯": tuple(180.0 + index * 3 for index in range(len(periods))),
        "리피토": tuple(150.0 + index for index in range(len(periods))),
        "리바로젯": tuple(90.0 + index * 4 for index in range(len(periods))),
        "아토젯": tuple(100.0 + index * 2 for index in range(len(periods))),
        "로수바미브": tuple(80.0 + index * 3 for index in range(len(periods))),
    }
    totals = tuple(sum(series[index] for series in values.values()) for index in range(len(periods)))
    layer = StrategicQueryLayer(
        reader=StaticStrategicMartReader(
            tuple(_record(brand, series, periods, totals) for brand, series in values.items())
        )
    )
    result = ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=BrandResolver(mode="fixture"),
        query_layer=layer,
    ).answer("리바로젯 경쟁사 성장률 표")

    yoy_calls = [
        call
        for call in result["tool_calls"]
        if call.get("render_data", {}).get("metric") == "yoy_growth"
    ]
    assert [call["render_data"]["brand"] for call in yoy_calls] == list(values)
    assert all(call["render_data"]["from_period"] == "2025-06" for call in yoy_calls)
    assert all(call["render_data"]["to_period"] == "2026-06" for call in yoy_calls)
    assert all(call["render_data"]["growth_pct"] is not None for call in yoy_calls)
    answer = fallback_fact_answer(
        {"fact_md": answer_fact_markdown(result["tool_calls"], result["sources"])},
        question="리바로젯 경쟁사 성장률 표",
    )
    assert "| 성장률(YoY, 2026-06 대비 2025-06) |" in answer
    assert answer.count("% |") >= 5
    assert "요청 지표 미제공" not in answer


def test_standard_structured_market_batch_overlaps_three_tools_and_records_trace(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_BQ_PARALLEL_TOOL_WORKERS", "3")
    layer = _layer()
    original = AgentToolFacade._execute
    independent = {"get_brand_share", "get_brand_sales", "get_brand_series", "get_top_brands"}
    state = {"active": 0, "peak": 0}
    lock = Lock()

    def tracked(self, name, arguments):
        if name not in independent:
            return original(self, name, arguments)
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            sleep(0.04)
            return original(self, name, arguments)
        finally:
            with lock:
                state["active"] -= 1

    monkeypatch.setattr(AgentToolFacade, "_execute", tracked)
    result = ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=BrandResolver(mode="fixture"),
        query_layer=layer,
    ).answer("리바로 최근 시장점유율 추이")

    traces = [call["qa_trace"] for call in result["tool_calls"] if isinstance(call.get("qa_trace"), dict)]
    assert state["peak"] == 3
    assert len(traces) == 4
    assert _trace_peak(traces) == 3


def test_standard_structured_parallel_batch_isolates_one_tool_exception(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_BQ_PARALLEL_TOOL_WORKERS", "3")
    layer = _layer()
    original = AgentToolFacade._execute

    def fail_sales(self, name, arguments):
        if name == "get_brand_sales":
            raise TimeoutError("injected sales timeout")
        return original(self, name, arguments)

    monkeypatch.setattr(AgentToolFacade, "_execute", fail_sales)
    result = ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=BrandResolver(mode="fixture"),
        query_layer=layer,
    ).answer("리바로 최근 시장점유율 추이")

    observations = result["agent_trace"][0]["observations"]
    failed = [item for item in observations if item["tool_name"] == "get_brand_sales"]
    succeeded = [item for item in observations if item["tool_name"] != "get_brand_sales"]
    assert len(failed) == 1
    assert failed[0]["call"]["render_data"]["status"] == "query_failed"
    assert failed[0]["call"]["qa_trace"]["status"] == "query_failed"
    assert len(succeeded) == 3
    assert all(item["status"] == "ok" for item in succeeded)
    assert result["answer"].strip()


def test_explicit_quarter_sales_bypasses_injected_llm_planner() -> None:
    layer = _layer()
    agent = ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=BrandResolver(mode="fixture"),
        planner=_PlannerBomb(),
        query_layer=layer,
    )

    result = agent.answer("리바로 2026년 1분기 매출")

    metrics = result["agent_loop_metrics"]
    assert metrics["deterministic_plan_hit"] is True
    assert metrics["deterministic_plan_kind"] == "brand_sales"
    assert metrics["llm_plan_calls"] == 0
    assert metrics["selected_tools"] == ["get_brand_sales"]


def test_explicit_missing_period_stops_with_typed_no_data_without_llm(monkeypatch) -> None:
    layer = _layer()
    monkeypatch.setattr(
        GenosToolPlanner,
        "decide",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("explicit missing period must not enter the LLM planner")
        ),
    )
    agent = ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=BrandResolver(mode="fixture"),
        query_layer=layer,
    )

    result = agent.answer("리바로 2035-01 매출")

    assert result["agent_loop_metrics"]["llm_plan_calls"] == 0
    assert result["router_diagnostics"]["gate"] == "typed_unavailable"
    assert result["decomposition"][0]["status"] == "no_data"
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["status"] == "no_data"
    assert result["tool_calls"][0]["render_data"]["status"] == "no_data"
    assert "요청하신 2035-01 데이터를 조회할 수 없습니다" in result["answer"]
    assert "다른 기간 값으로 대체하지 않습니다" in result["answer"]


def test_feature_flag_off_preserves_legacy_planner_path(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_DETERMINISTIC_MARKET_PLANNER_ENABLED", "false")
    monkeypatch.delenv("GENOS_TOKEN", raising=False)
    layer = _layer()
    agent = ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=BrandResolver(mode="fixture"),
        query_layer=layer,
    )

    result = agent.answer("리바로 최근 시장점유율 추이")

    assert result["agent_loop_metrics"]["deterministic_plan_hit"] is False
    assert result["agent_loop_metrics"]["llm_plan_calls"] >= 1


def test_structured_market_question_bypasses_llm_question_router() -> None:
    class RouterBomb:
        def route(self, _question: str, has_documents: bool = False):
            raise AssertionError("structured market question must bypass LLM decomposition")

    layer = _layer()
    agent = ChatAgent(
        router=RouterBomb(),
        resolver=BrandResolver(mode="fixture"),
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        query_layer=layer,
    )

    result = agent.answer("리바로 최근 시장점유율 추이")

    assert result["agent_loop_metrics"]["deterministic_plan_hit"] is True
    assert result["router_diagnostics"]["question_decomposition_bypassed"] is True


def test_requested_unavailable_source_precedes_structured_market_preflight() -> None:
    layer = _layer()
    agent = ChatAgent(
        resolver=BrandResolver(mode="fixture"),
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        query_layer=layer,
    )

    result = agent.answer("리바로 Cortellis 매출 알려줘")

    assert [call.get("tool") for call in result["tool_calls"]] == ["requested_source_unavailable"]
    assert result.get("router_diagnostics", {}).get("question_decomposition_bypassed") is not True


def test_companion_evidence_renders_one_combined_series_table() -> None:
    layer = _layer()
    agent = ChatAgent(
        resolver=BrandResolver(mode="fixture"),
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        query_layer=layer,
    )

    result = agent.answer("리바로 최근 시장점유율 추이")

    assert result["answer"].count("| 기간 | 시장점유율(%) | 처방조제액(억원) | 시장규모(억원) |") == 1
    assert result["answer"].count("### 분석 기준별 점유율") == 1
    assert "| 지표 | 값 |" not in result["answer"]
    assert "| 지표 | 수치(단위 포함) |" in result["answer"]
    assert "지표 수치는 데이터 표에서 한 번만 확인할 수 있습니다" not in result["answer"]
    assert result["agent_loop_metrics"]["tool_calls"] == 4


@pytest.mark.parametrize(
    "question",
    ("리바로 매출 추이", "리바로 시장 상위 5개", "리바로 시장 HHI"),
)
def test_structured_insight_tables_never_use_generic_value_headers(question: str) -> None:
    layer = _layer()
    agent = ChatAgent(
        resolver=BrandResolver(mode="fixture"),
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        query_layer=layer,
    )

    answer = agent.answer(question)["answer"]

    assert "| 지표 | 값 |" not in answer
    assert "| 지표 | 수치(단위 포함) |" in answer


def test_structured_market_answer_is_fast_deterministic_and_llm_free_for_five_runs() -> None:
    layer = _layer()
    agent = ChatAgent(
        resolver=BrandResolver(mode="fixture"),
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        query_layer=layer,
    )
    answers: list[str] = []
    elapsed: list[float] = []
    for _ in range(5):
        started = perf_counter()
        result = agent.answer("리바로 최근 시장점유율 추이")
        elapsed.append(perf_counter() - started)
        answers.append(result["answer"])
        assert result["agent_loop_metrics"]["llm_plan_calls"] == 0
        assert result["agent_loop_metrics"]["tool_calls"] == 4

    assert max(elapsed) < 2.0
    assert len(set(answers)) == 1


def test_structured_answers_and_prescription_stops_keep_verified_contracts() -> None:
    layer = _layer()
    agent = ChatAgent(
        resolver=BrandResolver(mode="fixture"),
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        query_layer=layer,
    )
    structured_questions = (
        "리바로 최근 시장점유율 추이",
        "리바로 시장점유율",
        "리바로 2026-03 점유율",
        "가드렛 점유율 추이",
        "가드렛 시장점유율",
        "리바로 매출 추이",
        "리바로 2026-03 매출",
        "가드렛 매출 추이",
        "리바로 성장률",
        "가드렛 성장률",
        "리바로 순위",
        "가드렛 순위 추이",
        "리바로 시장 상위 5개",
        "가드렛 상위 10개 브랜드",
        "리바로 시장 HHI",
        "가드렛 시장 집중도",
        "리바로 시장 규모",
        "리바로와 가드렛 비교",
    )

    for question in structured_questions:
        result = agent.answer(question)
        assert result["agent_loop_metrics"]["deterministic_plan_hit"] is True
        assert result["agent_loop_metrics"]["llm_plan_calls"] == 0
        assert result["markdown_response"]["verification"]["status"] == "pass"
        assert forbidden_claims(result["answer"]) == ()
        facts = evidence_from_calls(
            result["tool_calls"],
            result["markdown_response"]["data_md"],
        )
        assert verify_insight_answer(
            gate="G4",
            markdown=result["answer"],
            facts=facts,
            environment="fixture-20",
        ).exit_code == 0

    for question in ("리바로 처방조제액", "가드렛 처방조제액"):
        result = agent.answer(question)
        assert result["status"] == "unavailable"
        assert result["reason_code"] == "FIELD_NOT_EXPOSED"
        assert result["value"] is None
        assert result["tool_calls"] == []
        assert result["proxy"]["substituted"] is False


def _layer() -> StrategicQueryLayer:
    periods = ("2026-01", "2026-02", "2026-03")
    values = {
        "가드렛": (120.0, 125.0, 130.0),
        "리바로": (80.0, 82.0, 84.0),
    }
    totals = tuple(sum(series[index] for series in values.values()) for index in range(len(periods)))
    records = tuple(_record(brand, series, periods, totals) for brand, series in values.items())
    return StrategicQueryLayer(reader=StaticStrategicMartReader(records))


def _trace_peak(traces: list[dict[str, object]]) -> int:
    events: list[tuple[datetime, int]] = []
    for trace in traces:
        events.append((datetime.fromisoformat(str(trace["started_at"])), 1))
        events.append((datetime.fromisoformat(str(trace["ended_at"])), -1))
    active = 0
    peak = 0
    for _, delta in sorted(events, key=lambda item: (item[0], -item[1])):
        active += delta
        peak = max(peak, active)
    return peak


def _record(
    brand: str,
    values: tuple[float, ...],
    periods: tuple[str, ...],
    totals: tuple[float, ...],
) -> MartRecord:
    history = {
        period: {
            "raw_value": values[index] * 100_000_000,
            "ms": values[index] / totals[index] * 100,
            "source_status": "OK",
        }
        for index, period in enumerate(periods)
    }
    return MartRecord(
        ml_id="ml_006",
        brand_name=brand,
        source="ubist",
        measure="sales",
        metric_history=history,
        channel_data={},
        specialty_data={},
        dimension_data={},
        by_dimension={"company": "테스트제약", "molecule": f"{brand}성분"},
    )
