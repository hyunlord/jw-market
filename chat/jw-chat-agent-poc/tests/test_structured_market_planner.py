from __future__ import annotations

import pytest

from jw_chat_agent_poc.agent_loop.periods import build_period_grounding
from jw_chat_agent_poc.agent_loop.schemas import tool_schemas
from jw_chat_agent_poc.agent_loop.structured_planner import plan_structured_market_question
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tools.query_layer.catalog import default_catalog


STRUCTURED_QUESTIONS = (
    "리바로 최근 시장점유율 추이",
    "리바로 매출 추이",
    "리바로 성장률",
    "리바로 순위",
    "리바로 채널별 실적",
    "리바로 진료과별 실적",
    "리바로 시장 상위 5개",
    "리바로 시장 HHI",
    "리바로 시장 규모",
    "리바로와 가드렛 비교",
    "가드렛 점유율",
    "가드렛 처방조제액",
    "리바로젯 점유율 변화",
    "악템라 매출",
    "헴리브라 순위 추이",
    "리바로 2026-04 점유율",
    "리바로와 리바로젯 매출 비교",
    "가드렛 상위 10개 브랜드",
    "악템라 채널 구성",
    "헴리브라 진료과 구성",
)


def test_structured_slot_planner_hits_at_least_seventy_percent_without_llm() -> None:
    resolver = BrandResolver(mode="fixture")
    hits = []
    for question in STRUCTURED_QUESTIONS:
        grounding = build_period_grounding(question, current_month=lambda: "2026-06")
        try:
            brands = tuple(item.canonical_brand for item in resolver.resolve_many(question, allow_default=False))
        except LookupError:
            brands = ()
        schemas = tool_schemas(brands, grounding.schema_periods, default_catalog())
        plan = plan_structured_market_question(question, resolver, grounding, schemas)
        hits.append(plan is not None)

    assert sum(hits) >= 14
    assert sum(hits) / len(hits) >= 0.70


def test_share_plan_expands_to_sales_market_rank_and_series_evidence() -> None:
    question = "리바로 최근 시장점유율 추이"
    resolver = BrandResolver(mode="fixture")
    grounding = build_period_grounding(question, current_month=lambda: "2026-06")
    schemas = tool_schemas(("리바로",), grounding.schema_periods, default_catalog())

    plan = plan_structured_market_question(question, resolver, grounding, schemas)

    assert plan is not None
    assert plan.kind == "brand_share"
    assert {call.name for call in plan.decision.tool_calls} == {
        "get_brand_share",
        "get_brand_sales",
        "get_brand_series",
        "get_top_brands",
    }


def test_external_or_unstructured_question_falls_back_instead_of_false_hit() -> None:
    question = "리바로 최신 가이드라인 근거를 찾아줘"
    resolver = BrandResolver(mode="fixture")
    grounding = build_period_grounding(question, current_month=lambda: "2026-06")
    schemas = tool_schemas(("리바로",), grounding.schema_periods, default_catalog())

    assert plan_structured_market_question(question, resolver, grounding, schemas) is None


def test_explanatory_metric_question_is_not_misclassified_as_descriptive() -> None:
    question = "리바로 매출 영향 분석"
    resolver = BrandResolver(mode="fixture")
    grounding = build_period_grounding(question, current_month=lambda: "2026-06")
    schemas = tool_schemas(("리바로",), grounding.schema_periods, default_catalog())

    assert plan_structured_market_question(question, resolver, grounding, schemas) is None


@pytest.mark.parametrize(
    ("question", "expected_period"),
    (
        ("리바로 2025년 4월 매출", "2025-04"),
        ("리바로 2025년 2분기 매출", "2025-Q2"),
    ),
)
def test_structured_plan_preserves_canonical_explicit_period(
    question: str,
    expected_period: str,
) -> None:
    # Given: a user-facing Korean period and the production planner catalog.
    resolver = BrandResolver(mode="fixture")
    grounding = build_period_grounding(question, current_month=lambda: "2026-06")
    schemas = tool_schemas(("리바로",), grounding.schema_periods, default_catalog())

    # When: the deterministic market planner builds its tool calls.
    plan = plan_structured_market_question(question, resolver, grounding, schemas)

    # Then: every period-bearing tool receives the explicit canonical period.
    assert plan is not None
    period_arguments = {
        call.name: call.arguments["period"]
        for call in plan.decision.tool_calls
        if "period" in call.arguments
    }
    assert period_arguments
    assert set(period_arguments.values()) == {expected_period}
    assert expected_period in grounding.schema_periods


def test_query_tool_descriptions_require_context_companions() -> None:
    schemas = tool_schemas(
        ("리바로",),
        build_period_grounding("").schema_periods,
        default_catalog(),
    )
    descriptions = {
        item["function"]["name"]: item["function"]["description"]
        for item in schemas
    }

    assert "매출" in descriptions["get_brand_share"]
    assert "시장규모" in descriptions["get_brand_share"]
    assert "순위" in descriptions["get_brand_share"]
