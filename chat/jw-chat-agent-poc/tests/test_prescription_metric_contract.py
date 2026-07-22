from __future__ import annotations

import pytest

import jw_chat_agent_poc.orchestrator.agent as agent_module
from jw_chat_agent_poc.agent_loop.bq_planner import plan_bq_question
from jw_chat_agent_poc.agent_loop.bq_slots import extract_bq_slots
from jw_chat_agent_poc.agent_loop.periods import build_period_grounding
from jw_chat_agent_poc.agent_loop.schemas import tool_schemas
from jw_chat_agent_poc.orchestrator.agent import ChatAgent
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tools.query_layer.catalog import default_catalog


PRESCRIPTION_QUESTIONS = (
    "리바로젯 진료과별 처방 추이",
    "리바로 유통채널별 처방량",
    "처방건수 기준으로 알려줘",
    "리바로 처방조제액 추이",
)


@pytest.mark.parametrize("question", PRESCRIPTION_QUESTIONS)
def test_prescription_wording_never_returns_sales_as_a_substitute(
    question: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "false")
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "OFF")

    result = ChatAgent(external_mode="fixture").answer(question)

    assert result["status"] == "unavailable"
    assert result["reason_code"] == "FIELD_NOT_EXPOSED"
    assert result["value"] is None
    assert result["tool_calls"] == []
    assert result["proxy"]["metric"] == "sales"
    assert result["proxy"]["substituted"] is False
    assert "처방 지표가 아닙니다" in result["answer"]
    assert "1. 미보유 데이터" in result["answer"]
    assert "2. 현재 가능한 proxy" in result["answer"]
    assert "3. 해석 가능한 상한선" in result["answer"]
    assert "4. 확인 필요 데이터" in result["answer"]
    assert "5. 확보 시 수행할 분석" in result["answer"]
    assert "매출 추이는" not in result["answer"]


def test_prescription_slots_are_not_owned_by_sales() -> None:
    slots = extract_bq_slots(
        "리바로 처방조제액 추이",
        brand="리바로",
        period="latest",
    )

    assert "prescription" in slots.metrics
    assert "sales" not in slots.metrics


@pytest.mark.parametrize(
    "question",
    (
        "리바로 조제 추이",
        "Livalo prescription trend",
    ),
)
def test_all_prescription_vocabulary_is_owned_by_prescription(question: str) -> None:
    slots = extract_bq_slots(question, brand="리바로", period="latest")

    assert "prescription" in slots.metrics
    assert "sales" not in slots.metrics


def test_forced_sales_fallback_cannot_run_for_prescription_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "false")
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "OFF")
    monkeypatch.setattr(
        agent_module,
        "preflight_bq_question",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("sales fallback must be blocked before planning")
        ),
    )

    result = ChatAgent(external_mode="fixture", query_layer=object()).answer(
        "리바로 유통채널별 처방량"
    )

    assert result["reason_code"] == "FIELD_NOT_EXPOSED"
    assert result["tool_calls"] == []


def test_explicit_sales_does_not_hide_a_requested_prescription_metric() -> None:
    result = ChatAgent(external_mode="fixture").answer(
        "리바로 최근 매출과 처방 추이를 알려줘"
    )

    assert result["reason_code"] == "FIELD_NOT_EXPOSED"
    assert result["value"] is None
    assert result["tool_calls"] == []
    assert result["proxy"] == {
        "metric": "sales",
        "status": "separate_request_only",
        "substituted": False,
    }


def test_requested_source_unavailable_takes_precedence_over_prescription_guard() -> None:
    result = ChatAgent(external_mode="fixture").answer(
        "리바로 KOL 자문 기준 처방 의견과 시장 시사점을 알려줘"
    )

    assert result["answer"].startswith(
        "KOL 자문 데이터는 현재 운영 데이터에 미보유입니다."
    )
    assert [call.get("tool") for call in result["tool_calls"]] == [
        "requested_source_unavailable"
    ]


def test_explicit_sales_channel_question_keeps_existing_c2_plan() -> None:
    question = "리바로젯 채널별 매출"
    resolver = BrandResolver(mode="fixture")
    grounding = build_period_grounding(question, current_month=lambda: "2026-06")
    schemas = tool_schemas(("리바로젯",), grounding.schema_periods, default_catalog())

    plan = plan_bq_question(question, resolver, grounding, schemas)

    assert plan is not None
    assert plan.contract.contract_id == "C2"
    assert plan.slots.metrics == ("sales",)
    assert [call.name for call in plan.decision.tool_calls] == [
        "get_brand_channel_breakdown",
        "get_brand_specialty_breakdown",
        "get_brand_series",
    ]
