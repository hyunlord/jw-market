from __future__ import annotations

import pytest

from jw_chat_agent_poc.agent_loop.bq_planner import plan_bq_question
from jw_chat_agent_poc.agent_loop.periods import build_period_grounding
from jw_chat_agent_poc.agent_loop.schemas import tool_schemas
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tools.query_layer.catalog import default_catalog


BQ_QUESTIONS = (
    ("A1", "리바로 시장 규모가 지금 얼마고 어떻게 변해왔어?"),
    ("A2", "리바로 시장 앞으로 어떻게 될 것 같아?"),
    ("A3", "리바로 질병 환자수랑 최근 매출 한번에"),
    ("B1", "리바로 시장 경쟁 구도가 최근 어떻게 변하고 있어?"),
    ("B2", "리바로 경쟁 상대는 누구고 우리 위치는 어디야?"),
    ("B3", "리바로 신규 진입자나 위협 브랜드 있어?"),
    ("C1", "리바로 최근 매출 처방 추이 어때?"),
    ("C2", "리바로 어느 채널 진료과에서 잘 팔려?"),
    ("C3", "리바로 IQVIA랑 UBIST 수치가 다른데 왜?"),
    ("D1", "리바로 영업활동 추이 어때?"),
    ("D2", "리바로 영업활동이 매출에 영향 줬어?"),
    ("D3", "리바로 경쟁사 영업활동 변화 있어?"),
    ("E1", "리바로 관련 최근 이슈 뭐 있어?"),
    ("E2", "리바로 왜 이렇게 됐어?"),
)


def _plan(question: str):
    resolver = BrandResolver(mode="fixture")
    grounding = build_period_grounding(question, current_month=lambda: "2026-06")
    schemas = tool_schemas(("리바로",), grounding.schema_periods, default_catalog())
    return plan_bq_question(question, resolver, grounding, schemas)


@pytest.mark.parametrize(("contract_id", "question"), BQ_QUESTIONS)
def test_every_defined_bq_question_has_a_deterministic_plan(
    contract_id: str,
    question: str,
) -> None:
    plan = _plan(question)

    assert plan is not None
    assert plan.contract.contract_id == contract_id
    assert plan.decision.tool_calls
    assert all(call.reason == f"BQ contract {contract_id}" for call in plan.decision.tool_calls)


def test_planner_records_semantic_slots_before_contract_selection() -> None:
    market_plan = _plan("리바로 시장 규모가 지금 얼마고 어떻게 변해왔어?")
    axis_plan = _plan("리바로 어느 채널 진료과에서 잘 팔려?")
    source_plan = _plan("리바로 IQVIA랑 UBIST 수치가 다른데 왜?")

    assert market_plan is not None
    assert market_plan.slots.metrics == ("market", "market_size")
    assert market_plan.slots.modifiers == ("trend",)
    assert axis_plan is not None
    assert axis_plan.slots.axes == ("channel", "specialty")
    assert source_plan is not None
    assert source_plan.slots.sources == ("ubist", "iqvia_nsa")


def test_iqvia_ubist_comparison_creates_separate_calls_without_sum() -> None:
    plan = _plan("리바로 IQVIA랑 UBIST 수치가 다른데 왜?")

    assert plan is not None
    calls = plan.decision.tool_calls
    assert [(call.name, call.arguments.get("source")) for call in calls] == [
        ("get_brand_series", "ubist"),
        ("get_brand_series", "iqvia_nsa"),
    ]
    assert all("sum" not in call.name.casefold() for call in calls)


def test_all_source_causal_plan_is_evidence_first() -> None:
    plan = _plan("리바로 왜 이렇게 됐어?")

    assert plan is not None
    names = {call.name for call in plan.decision.tool_calls}
    assert {
        "get_brand_series",
        "get_top_brands",
        "search_news",
        "csd_activity_trend",
        "get_disease_stats",
    }.issubset(names)


def test_unmatched_question_is_not_silently_substituted() -> None:
    assert _plan("리바로 사내 미지원 원천을 대신 찾아줘") is None


def test_required_market_source_gap_is_explicit_in_plan() -> None:
    resolver = BrandResolver(mode="fixture")
    question = "리바로 IQVIA랑 UBIST 수치가 다른데 왜?"
    grounding = build_period_grounding(question, current_month=lambda: "2026-06")
    schemas = tool_schemas(("리바로",), grounding.schema_periods, default_catalog())

    plan = plan_bq_question(
        question,
        resolver,
        grounding,
        schemas,
        available_sources=("ubist",),
    )

    assert plan is not None
    assert plan.missing_sources == ("iqvia_nsa",)
    assert [call.arguments.get("source") for call in plan.decision.tool_calls] == ["ubist"]
