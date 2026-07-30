from __future__ import annotations

from copy import deepcopy
import logging

import pytest

from jw_chat_agent_poc.agent_loop.periods import build_period_grounding
from jw_chat_agent_poc.agent_loop.models import ToolCallPlan
from jw_chat_agent_poc.orchestrator.operation_contract import (
    CoverageDecisionStatus,
    coverage_decision_observation,
    evaluate_actual_coverage,
    evaluate_plan_coverage,
    observe_actual_coverage,
    observe_plan_coverage,
)
from jw_chat_agent_poc.orchestrator.period_selection import (
    PeriodGrain,
    PeriodResolution,
    period_selection_for_spec,
)
from jw_chat_agent_poc.orchestrator.query_spec import (
    EntityKind,
    QueryEntity,
    QueryOperation,
    RequestQuerySpec,
    TimeGranularity,
    extract_query_spec,
)
from jw_chat_agent_poc.resolver import BrandResolution
from jw_chat_agent_poc.service import app as service_app


class _PeriodAcceptanceResolver:
    def resolve_many(
        self,
        question_or_brands: str,
        allow_default: bool = False,
    ) -> tuple[BrandResolution, ...]:
        del allow_default
        return tuple(
            BrandResolution(
                canonical_brand=brand,
                audit_code=f"test:{brand}",
                molecule_en=(),
                atc=(),
                edi_code=None,
                item_seq=None,
                is_combo=False,
            )
            for brand in ("리바로", "아일리아")
            if brand in question_or_brands
        )


def _sales_spec(
    *,
    operation: QueryOperation = QueryOperation.CURRENT_VALUE,
    start_period: str | None = None,
    end_period: str | None = None,
    window_count: int | None = None,
    granularity: TimeGranularity | None = None,
) -> RequestQuerySpec:
    return RequestQuerySpec(
        entities=(
            QueryEntity(
                kind=EntityKind.BRAND,
                canonical_id="리바로",
                display_name="리바로",
            ),
        ),
        operation=operation,
        metrics=("sales",),
        start_period=start_period,
        end_period=end_period,
        window_count=window_count,
        granularity=granularity,
    )


def _sales_plan(period: str) -> tuple[ToolCallPlan, ...]:
    return (
        ToolCallPlan(
            name="get_brand_sales",
            arguments={"brand": "리바로", "period": period},
            reason="live-shaped structured plan",
        ),
    )


@pytest.mark.parametrize(
    ("question", "expected"),
    (
        (
            "리바로 2025년 1월부터 2025년 12월까지 매출 알려줘",
            {
                "operation": QueryOperation.CURRENT_VALUE,
                "start_period": "2025-01",
                "end_period": "2025-12",
                "window_count": None,
                "granularity": None,
            },
        ),
        (
            "아일리아 최근 4개 분기 매출 알려줘",
            {
                "operation": QueryOperation.TIME_SERIES,
                "start_period": None,
                "end_period": None,
                "window_count": 4,
                "granularity": TimeGranularity.QUARTER,
            },
        ),
    ),
)
def test_live_questions_reach_expected_query_spec_shape(
    question: str,
    expected: dict[str, object],
) -> None:
    spec = extract_query_spec(
        question,
        _PeriodAcceptanceResolver(),
        build_period_grounding(question),
    )

    assert len(spec.entities) == 1
    assert spec.metrics == ("sales",)
    for field, value in expected.items():
        assert getattr(spec, field) == value


def test_pr04_closed_month_range_reports_eleven_missing_periods() -> None:
    # Given
    spec = _sales_spec(start_period="2025-01", end_period="2025-12")

    # When
    decision = evaluate_plan_coverage(spec, _sales_plan("2025-01"))
    observation = coverage_decision_observation(decision)

    # Then
    assert decision.status is CoverageDecisionStatus.FAIL
    assert observation["period_set"] == {
        "status": "missing",
        "kind": "closed_range",
        "grain": "month",
        "resolution": "resolved",
        "expected_count": 12,
        "observed_count": 1,
        "missing_count": 11,
        "expected_periods": [f"2025-{month:02d}" for month in range(1, 13)],
        "observed_periods": ["2025-01"],
        "missing_periods": [f"2025-{month:02d}" for month in range(2, 13)],
        "anchor": None,
    }


def test_pr08_trailing_quarters_reports_three_missing_from_actual_payload() -> None:
    # Given
    spec = _sales_spec(
        operation=QueryOperation.TIME_SERIES,
        window_count=4,
        granularity=TimeGranularity.QUARTER,
    )
    calls = (
        {
            "tool": "get_brand_sales",
            "render_data": {
                "brand": "리바로",
                "period": "2026-Q1",
                "sales_억원": 80.39,
            },
        },
    )

    # When
    decision = evaluate_actual_coverage(spec, calls)
    observation = coverage_decision_observation(decision)

    # Then
    assert decision.status is CoverageDecisionStatus.FAIL
    assert observation["period_set"] == {
        "status": "missing",
        "kind": "trailing_window",
        "grain": "quarter",
        "resolution": "resolved",
        "expected_count": 4,
        "observed_count": 1,
        "missing_count": 3,
        "expected_periods": ["2025-Q2", "2025-Q3", "2025-Q4", "2026-Q1"],
        "observed_periods": ["2026-Q1"],
        "missing_periods": ["2025-Q2", "2025-Q3", "2025-Q4"],
        "anchor": "2026-Q1",
    }


def test_pr08_plan_without_canonical_anchor_is_unverifiable() -> None:
    # Given
    spec = _sales_spec(
        operation=QueryOperation.TIME_SERIES,
        window_count=4,
        granularity=TimeGranularity.QUARTER,
    )

    # When
    decision = evaluate_plan_coverage(spec, _sales_plan("latest"))
    observation = coverage_decision_observation(decision)

    # Then
    assert decision.status.value == "unverifiable"
    assert observation["period_set"] == {
        "status": "unverifiable",
        "kind": "trailing_window",
        "grain": "quarter",
        "resolution": "unverifiable",
        "expected_count": 4,
        "observed_count": 0,
        "missing_count": 0,
        "expected_periods": [],
        "observed_periods": [],
        "missing_periods": [],
        "anchor": None,
    }


@pytest.mark.parametrize(
    ("start_period", "end_period", "expected_status"),
    (
        ("2025-12", "2025-01", "invalid"),
        ("2025-01", "2025-Q4", "invalid"),
        ("2020-01", "2025-01", "unverifiable"),
    ),
)
def test_invalid_or_oversized_ranges_never_pass(
    start_period: str,
    end_period: str,
    expected_status: str,
) -> None:
    spec = _sales_spec(start_period=start_period, end_period=end_period)

    decision = evaluate_plan_coverage(spec, _sales_plan(start_period))
    observation = coverage_decision_observation(decision)

    assert decision.status.value == expected_status
    assert observation["period_set"] != "N/A"
    assert observation["period_set"]["status"] == expected_status
    assert observation["period_set"]["resolution"] == expected_status


def test_normalizer_supports_year_range_without_expanding_stage1_scope() -> None:
    spec = _sales_spec(start_period="2021", end_period="2024")

    selection = period_selection_for_spec(spec, ())
    decision = evaluate_plan_coverage(spec, _sales_plan("2021"))

    assert selection is not None
    assert selection.grain is PeriodGrain.YEAR
    assert selection.resolution is PeriodResolution.RESOLVED
    assert [period.value for period in selection.members] == [
        "2021",
        "2022",
        "2023",
        "2024",
    ]
    assert decision.status is CoverageDecisionStatus.NOT_APPLICABLE
    assert decision.reason == "period_range"


def test_other_quarter_window_remains_outside_stage1_scope() -> None:
    spec = _sales_spec(
        operation=QueryOperation.TIME_SERIES,
        window_count=3,
        granularity=TimeGranularity.QUARTER,
    )

    decision = evaluate_plan_coverage(spec, _sales_plan("2026-Q1"))

    assert decision.status is CoverageDecisionStatus.NOT_APPLICABLE
    assert decision.reason == "unsupported_operation"


def test_multiple_entities_or_metrics_remain_outside_period_set_scope() -> None:
    second_brand = QueryEntity(
        kind=EntityKind.BRAND,
        canonical_id="리바로젯",
        display_name="리바로젯",
    )
    base = _sales_spec(start_period="2025-01", end_period="2025-12")
    multi_entity = RequestQuerySpec(
        entities=(*base.entities, second_brand),
        operation=base.operation,
        metrics=base.metrics,
        start_period=base.start_period,
        end_period=base.end_period,
    )
    multi_metric = RequestQuerySpec(
        entities=base.entities,
        operation=base.operation,
        metrics=("sales", "share"),
        start_period=base.start_period,
        end_period=base.end_period,
    )

    assert evaluate_plan_coverage(
        multi_entity,
        _sales_plan("2025-01"),
    ).status is CoverageDecisionStatus.NOT_APPLICABLE
    assert evaluate_plan_coverage(
        multi_metric,
        _sales_plan("2025-01"),
    ).status is CoverageDecisionStatus.NOT_APPLICABLE


@pytest.mark.parametrize(
    ("question", "spec", "period"),
    (
        (
            "리바로 2025년 1월부터 2025년 12월까지 매출 알려줘",
            _sales_spec(start_period="2025-01", end_period="2025-12"),
            "2025-01",
        ),
        (
            "아일리아 최근 4개 분기 매출 알려줘",
            RequestQuerySpec(
                entities=(
                    QueryEntity(
                        kind=EntityKind.BRAND,
                        canonical_id="아일리아",
                        display_name="아일리아",
                    ),
                ),
                operation=QueryOperation.TIME_SERIES,
                metrics=("sales",),
                window_count=4,
                granularity=TimeGranularity.QUARTER,
            ),
            "2026-Q1",
        ),
    ),
)
def test_period_shadow_leaves_final_answer_byte_identical(
    question: str,
    spec: RequestQuerySpec,
    period: str,
) -> None:
    brand = spec.entities[0].canonical_id
    result = {
        "general_view_ready": True,
        "answer": f"{brand} {period} 매출은 80.39억원입니다.",
        "sources": ["UBIST"],
        "tool_calls": [
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "brand": brand,
                    "metric": "sales",
                    "period": period,
                    "sales_억원": 80.39,
                },
            }
        ],
    }
    baseline_result = deepcopy(result)
    observed_result = deepcopy(result)

    baseline = service_app.compute_final_answer(
        question,
        baseline_result,
        "baseline",
    )
    observed = service_app.compute_final_answer(
        question,
        observed_result,
        "observed",
        query_spec=spec,
    )

    assert observed.text.encode() == baseline.text.encode()
    assert observed_result["answer"] == baseline_result["answer"]
    assert observed_result["tool_calls"] == baseline_result["tool_calls"]


def test_shadow_logs_only_bounded_canonical_period_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = _sales_spec(start_period="2025-01", end_period="2025-12")
    plan = _sales_plan("2025-01")
    calls = (
        {
            "tool": "get_brand_sales",
            "render_data": {
                "brand": "리바로",
                "period": "2025-01",
                "sales_억원": 80.39,
            },
        },
    )

    with caplog.at_level(
        logging.INFO,
        logger="jw_chat_agent_poc.orchestrator.operation_contract",
    ):
        observe_plan_coverage(spec, plan, planner_kind="structured", step=1)
        observe_actual_coverage(spec, calls)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "expected_count': 12" in messages
    assert "observed_count': 1" in messages
    assert "missing_count': 11" in messages
    assert "2025-01" in messages
    assert "리바로 2025년 1월부터" not in messages
