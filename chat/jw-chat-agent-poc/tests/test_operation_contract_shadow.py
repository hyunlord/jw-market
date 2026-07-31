from __future__ import annotations

import logging
from dataclasses import FrozenInstanceError

import pytest
from fastapi.testclient import TestClient

from jw_chat_agent_poc.agent_loop import loop as agent_loop
from jw_chat_agent_poc.agent_loop.loop import ToolUseAgent
from jw_chat_agent_poc.agent_loop.models import AgentDecision, ToolCallPlan
from jw_chat_agent_poc.orchestrator.operation_contract import (
    CoverageDecisionStatus,
    clear_current_query_spec,
    coverage_decision_observation,
    current_query_spec,
    evaluate_actual_coverage,
    evaluate_plan_coverage,
    observe_actual_coverage,
    observe_plan_coverage,
    set_current_query_spec,
)
from jw_chat_agent_poc.orchestrator.query_spec import (
    EntityKind,
    QueryEntity,
    QueryOperation,
    RequestQuerySpec,
    TimeGranularity,
)
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.service import app as service_app

from test_service import _fake_agent_factory, _market_scope_resolver
from test_stage2_agent_loop import ScriptedPlanner, _metrics_tool


def _brand(name: str) -> QueryEntity:
    return QueryEntity(kind=EntityKind.BRAND, canonical_id=name, display_name=name)


def _spec(
    *brands: str,
    metrics: tuple[str, ...] = ("sales",),
    operation: QueryOperation = QueryOperation.CURRENT_VALUE,
    start_period: str | None = None,
    end_period: str | None = None,
    source: str | None = None,
    requested_view: str | None = None,
    window_count: int | None = None,
    granularity: TimeGranularity | None = None,
) -> RequestQuerySpec:
    entities = tuple(_brand(brand) for brand in brands)
    return RequestQuerySpec(
        entities=entities,
        operation=operation,
        metrics=metrics,
        start_period=start_period,
        end_period=end_period,
        window_count=window_count,
        granularity=granularity,
        comparison_targets=entities if operation is QueryOperation.COMPARE_CURRENT else (),
        source=source,
        requested_view=requested_view,
    )


def _metric(brand: str, measure: str, period: str = "latest") -> ToolCallPlan:
    return ToolCallPlan(
        name="get_metric",
        arguments={"brand": brand, "measure": measure, "period": period},
        reason="test plan",
    )


def test_shadow_fails_when_three_brand_sales_plan_covers_only_two() -> None:
    # Given
    spec = _spec(
        "리바로",
        "리바로젯",
        "로수젯",
        operation=QueryOperation.COMPARE_CURRENT,
    )

    # When
    decision = evaluate_plan_coverage(
        spec,
        (_metric("리바로", "sales"), _metric("리바로젯", "sales")),
    )

    # Then
    assert decision.status is CoverageDecisionStatus.FAIL
    assert tuple(axis.entity_id for axis in decision.missing) == ("로수젯",)


def test_shadow_passes_when_two_brand_sales_plan_covers_both() -> None:
    # Given
    spec = _spec("리바로", "리바로젯", operation=QueryOperation.COMPARE_CURRENT)

    # When
    decision = evaluate_plan_coverage(
        spec,
        (_metric("리바로", "sales"), _metric("리바로젯", "sales")),
    )

    # Then
    assert decision.status is CoverageDecisionStatus.PASS
    assert decision.missing == ()


def test_shadow_passes_for_single_brand_sales_without_mutating_plan() -> None:
    # Given
    spec = _spec("리바로")
    plan = (_metric("리바로", "sales"),)

    # When
    decision = evaluate_plan_coverage(spec, plan)

    # Then
    assert decision.status is CoverageDecisionStatus.PASS
    assert plan == (_metric("리바로", "sales"),)


def test_shadow_fails_when_sales_and_share_request_has_only_sales_plan() -> None:
    # Given
    spec = _spec("리바로", metrics=("sales", "share"))

    # When
    decision = evaluate_plan_coverage(spec, (_metric("리바로", "sales"),))

    # Then
    assert decision.status is CoverageDecisionStatus.FAIL
    assert tuple(axis.metric for axis in decision.missing) == ("share",)


def test_structured_comparison_plan_covers_both_brands_and_current_metrics() -> None:
    # Given
    spec = _spec(
        "리바로",
        "리바로젯",
        metrics=("sales", "share"),
        operation=QueryOperation.COMPARE_CURRENT,
    )
    plan = (
        ToolCallPlan(
            name="compare_brands_series",
            arguments={
                "brand": "리바로",
                "comparison_brand": "리바로젯",
                "period": "latest",
            },
            reason="structured comparison",
        ),
    )

    # When
    decision = evaluate_plan_coverage(spec, plan)

    # Then
    assert decision.status is CoverageDecisionStatus.PASS
    assert len(decision.observed) == 4


def test_structured_comparison_plan_does_not_claim_unplanned_rank_coverage() -> None:
    # Given
    spec = _spec(
        "리바로",
        "리바로젯",
        metrics=("rank",),
        operation=QueryOperation.COMPARE_CURRENT,
    )
    plan = (
        ToolCallPlan(
            name="compare_brands_series",
            arguments={
                "brand": "리바로",
                "comparison_brand": "리바로젯",
                "period": "latest",
            },
            reason="structured comparison",
        ),
    )

    # When
    decision = evaluate_plan_coverage(spec, plan)

    # Then
    assert decision.status is CoverageDecisionStatus.FAIL
    assert tuple(axis.metric for axis in decision.missing) == ("rank", "rank")


@pytest.mark.parametrize(
    ("spec", "reason"),
    (
        (_spec(metrics=("sales",)), "extractor_failure"),
        (_spec("리바로", metrics=()), "extractor_failure"),
        (_spec("리바로", source="hira"), "external_domain"),
        (_spec("리바로", requested_view="general_view"), "general_view"),
        (
            _spec(
                "리바로",
                operation=QueryOperation.TIME_SERIES,
                window_count=3,
                granularity=TimeGranularity.MONTH,
            ),
            "unsupported_operation",
        ),
        (
            _spec(
                "리바로",
                metrics=("share",),
                start_period="2025-01",
                end_period="2025-12",
            ),
            "period_range",
        ),
    ),
)
def test_shadow_marks_unsupported_or_empty_required_coverage_not_applicable(
    spec: RequestQuerySpec,
    reason: str,
) -> None:
    # When
    decision = evaluate_plan_coverage(spec, ())

    # Then
    assert decision.status is CoverageDecisionStatus.NOT_APPLICABLE
    assert decision.reason == reason
    assert decision.required == ()
    assert coverage_decision_observation(decision)["required"] == "N/A"


def test_coverage_decision_is_immutable() -> None:
    # Given
    decision = evaluate_plan_coverage(_spec("리바로"), (_metric("리바로", "sales"),))

    # When / Then
    with pytest.raises(FrozenInstanceError):
        setattr(decision, "reason", "changed")


def test_same_plan_has_identical_decision_for_all_planner_labels() -> None:
    # Given
    spec = _spec("리바로", "리바로젯", operation=QueryOperation.COMPARE_CURRENT)
    plans = {
        "bq": (_metric("리바로", "sales"),),
        "structured": (_metric("리바로", "sales"),),
        "heuristic": (_metric("리바로", "sales"),),
    }

    # When
    decisions = {
        label: evaluate_plan_coverage(spec, plan)
        for label, plan in plans.items()
    }

    # Then
    assert len(set(decisions.values())) == 1
    assert decisions["bq"].status is CoverageDecisionStatus.FAIL


def test_shadow_accepts_one_explicit_period_only_when_plan_matches() -> None:
    # Given
    spec = _spec("리바로", start_period="2025-Q2", end_period="2025-Q2")

    # When
    matching = evaluate_plan_coverage(spec, (_metric("리바로", "sales", "2025-Q2"),))
    mismatching = evaluate_plan_coverage(spec, (_metric("리바로", "sales", "latest"),))

    # Then
    assert matching.status is CoverageDecisionStatus.PASS
    assert mismatching.status is CoverageDecisionStatus.FAIL


def test_actual_coverage_uses_returned_values_and_normalizes_latest_period() -> None:
    # Given
    spec = _spec("리바로", metrics=("sales", "share", "rank"))
    calls = (
        {
            "tool": "get_brand_metric",
            "render_data": {
                "brand": "리바로",
                "period": "2026-05",
                "sales_억원": 80.39,
                "ms_recent_pct": 3.76,
                "rank": 6,
            },
        },
    )

    # When
    decision = evaluate_actual_coverage(spec, calls)

    # Then
    assert decision.status is CoverageDecisionStatus.PASS
    assert {axis.period for axis in decision.observed} == {"latest"}


def test_actual_coverage_does_not_count_failed_tool_values() -> None:
    # Given
    spec = _spec("리바로")
    calls = (
        {
            "tool": "get_brand_metric",
            "status": "query_failed",
            "render_data": {
                "brand": "리바로",
                "period": "2026-05",
                "sales_억원": 80.39,
            },
        },
    )

    # When
    decision = evaluate_actual_coverage(spec, calls)

    # Then
    assert decision.status is CoverageDecisionStatus.FAIL
    assert tuple(axis.entity_id for axis in decision.missing) == ("리바로",)


@pytest.mark.parametrize("missing_value", ("", "N/A", "—", "null", None))
def test_actual_coverage_does_not_count_missing_value_sentinels(
    missing_value: object,
) -> None:
    # Given
    spec = _spec("리바로")
    calls = (
        {
            "tool": "get_brand_metric",
            "render_data": {
                "brand": "리바로",
                "period": "2026-05",
                "sales_억원": missing_value,
            },
        },
    )

    # When
    decision = evaluate_actual_coverage(spec, calls)

    # Then
    assert decision.status is CoverageDecisionStatus.FAIL


def test_actual_coverage_counts_numeric_zero_as_real_value() -> None:
    # Given
    spec = _spec("리바로")
    calls = (
        {
            "tool": "get_brand_metric",
            "render_data": {
                "brand": "리바로",
                "period": "2026-05",
                "sales_억원": 0,
            },
        },
    )

    # When
    decision = evaluate_actual_coverage(spec, calls)

    # Then
    assert decision.status is CoverageDecisionStatus.PASS


def test_shadow_observers_log_internal_decisions_without_mutating_inputs(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setenv("JW_CHAT_OPERATION_CONTRACT_MODE", "SHADOW")
    monkeypatch.setenv("JW_CHAT_PERIOD_SET_CONTRACT_MODE", "SHADOW")
    spec = _spec("리바로")
    plan = (_metric("리바로", "sales"),)
    calls = (
        {
            "tool": "get_brand_metric",
            "render_data": {
                "brand": "리바로",
                "period": "2026-05",
                "sales_억원": 80.39,
            },
        },
    )

    # When
    with caplog.at_level(
        "INFO",
        logger="jw_chat_agent_poc.orchestrator.operation_contract",
    ):
        plan_decision = observe_plan_coverage(
            spec,
            plan,
            planner_kind="test",
            step=1,
        )
        actual_decision = observe_actual_coverage(spec, calls)

    # Then
    assert plan == (_metric("리바로", "sales"),)
    assert calls[0]["render_data"]["sales_억원"] == 80.39
    assert plan_decision.status is CoverageDecisionStatus.PASS
    assert actual_decision.status is CoverageDecisionStatus.PASS
    assert any("operation_contract_plan_shadow" in record.message for record in caplog.records)
    assert any("operation_contract_actual_shadow" in record.message for record in caplog.records)


def test_query_spec_context_is_internal_and_explicitly_cleared() -> None:
    # Given
    spec = _spec("리바로")
    clear_current_query_spec()

    # When
    set_current_query_spec(spec)

    # Then
    assert current_query_spec() is spec
    assert coverage_decision_observation(
        evaluate_plan_coverage(spec, (_metric("리바로", "sales"),))
    )["status"] == "pass"
    clear_current_query_spec()
    assert current_query_spec() is None


def test_agent_loop_decision_point_emits_plan_shadow_without_changing_result(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setenv("JW_CHAT_OPERATION_CONTRACT_MODE", "SHADOW")
    monkeypatch.setenv("JW_CHAT_PERIOD_SET_CONTRACT_MODE", "SHADOW")
    spec = _spec("리바로")
    planner = ScriptedPlanner(
        (
            AgentDecision(tool_calls=(_metric("리바로", "sales"),)),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    metrics = _metrics_tool()
    agent = ToolUseAgent(
        metrics=metrics,
        resolver=BrandResolver(),
        planner=planner,
    )
    set_current_query_spec(spec)

    # When
    try:
        with caplog.at_level(
            logging.INFO,
            logger="jw_chat_agent_poc.orchestrator.operation_contract",
        ):
            result = agent.answer("리바로 매출 알려줘")
    finally:
        clear_current_query_spec()

    # Then
    assert result["tool_calls"]
    assert any(
        "operation_contract_plan_shadow" in record.message
        and "'status': 'pass'" in record.message
        for record in caplog.records
    )
    assert not any("operation_contract" in key for key in result)


def test_compute_final_answer_emits_actual_shadow_with_byte_identical_answer(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setenv("JW_CHAT_OPERATION_CONTRACT_MODE", "SHADOW")
    monkeypatch.setenv("JW_CHAT_PERIOD_SET_CONTRACT_MODE", "SHADOW")
    question = "리바로 매출 알려줘"
    result = {
        "general_view_ready": True,
        "answer": "리바로 최신 매출은 80.39억원입니다.",
        "sources": ["UBIST"],
        "tool_calls": [
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "brand": "리바로",
                    "metric": "sales",
                    "period": "2026-05",
                    "sales_억원": 80.39,
                },
            }
        ],
    }
    clear_current_query_spec()
    baseline = service_app.compute_final_answer(question, result, "baseline")
    set_current_query_spec(_spec("리바로"))

    # When
    with caplog.at_level(
        logging.INFO,
        logger="jw_chat_agent_poc.orchestrator.operation_contract",
    ):
        observed = service_app.compute_final_answer(question, result, "observed")

    # Then
    assert observed.text.encode() == baseline.text.encode()
    assert current_query_spec() is None
    assert any(
        "operation_contract_actual_shadow" in record.message
        and "'status': 'pass'" in record.message
        for record in caplog.records
    )


def test_plan_shadow_failure_does_not_change_agent_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    planner = ScriptedPlanner(
        (
            AgentDecision(tool_calls=(_metric("리바로", "sales"),)),
            AgentDecision(final_answer="도구 결과로 답변"),
        )
    )
    metrics = _metrics_tool()
    agent = ToolUseAgent(
        metrics=metrics,
        resolver=BrandResolver(),
        planner=planner,
    )

    def _fail_shadow(*_args, **_kwargs):
        raise RuntimeError("synthetic plan shadow failure")

    monkeypatch.setattr(agent_loop, "observe_plan_coverage", _fail_shadow)
    set_current_query_spec(_spec("리바로"))

    # When
    try:
        result = agent.answer("리바로 매출 알려줘")
    finally:
        clear_current_query_spec()

    # Then
    assert result["answer"]
    assert result["tool_calls"]


def test_actual_shadow_failure_does_not_change_final_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    question = "리바로 매출 알려줘"
    result = {
        "general_view_ready": True,
        "answer": "리바로 최신 매출은 80.39억원입니다.",
        "sources": ["UBIST"],
        "tool_calls": [],
    }
    clear_current_query_spec()
    baseline = service_app.compute_final_answer(question, result, "baseline")

    def _fail_shadow(*_args, **_kwargs):
        raise RuntimeError("synthetic actual shadow failure")

    monkeypatch.setattr(service_app, "observe_actual_coverage", _fail_shadow)
    set_current_query_spec(_spec("리바로"))

    # When
    observed = service_app.compute_final_answer(question, result, "observed")

    # Then
    assert observed.text.encode() == baseline.text.encode()
    assert current_query_spec() is None


def test_chat_session_replay_carries_internal_query_spec_without_public_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    store = service_app.SessionStore()
    app = service_app.create_app(
        agent_factory=_fake_agent_factory,
        market_scope_resolver=_market_scope_resolver(),
        store=store,
    )
    client = TestClient(app)
    accepted = client.post("/chat", json={"question": "리바로 매출 알려줘"})
    session_id = accepted.json()["session_id"]
    stored = store.get(session_id)
    captured: list[RequestQuerySpec | None] = []

    def _final_answer(
        _question: str,
        _result: dict,
        conversation_id: str | None = None,
        *,
        query_spec: RequestQuerySpec | None = None,
    ) -> service_app.FinalAnswer:
        captured.append(query_spec)
        return service_app.FinalAnswer(
            text="리바로 최신 매출입니다.",
            charts=[],
            timing={},
            trace={},
            sources=("cache",),
            conversation_id=conversation_id,
        )

    monkeypatch.setattr(service_app, "compute_final_answer", _final_answer)

    # When
    replay = client.get("/chat/stream", params={"session_id": session_id})

    # Then
    assert accepted.status_code == 200
    assert replay.status_code == 200
    assert stored is not None
    assert not any("query_spec" in key for key in stored)
    assert captured and captured[0] is not None
    assert captured[0].operation is QueryOperation.CURRENT_VALUE
    assert current_query_spec() is None
