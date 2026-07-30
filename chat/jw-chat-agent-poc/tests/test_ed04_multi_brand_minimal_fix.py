from __future__ import annotations

import pytest

from jw_chat_agent_poc.agent_loop.bq_planner import plan_bq_question
from jw_chat_agent_poc.agent_loop.periods import build_period_grounding
from jw_chat_agent_poc.agent_loop.planner import BrandUnresolvedError, _brand
from jw_chat_agent_poc.agent_loop.schemas import tool_schemas
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.tools.query_layer.catalog import default_catalog


ED04_QUESTION = (
    "리바로와 리바로젯과 로수젯과 리피토의 최근 3년 매출, 점유율, 순위, "
    "시장 규모, HHI 변화를 연도별로 비교하고 각 브랜드의 성장 원인과 "
    "경쟁 구도까지 한 번에 설명해줘"
)
MATCHED_BRANDS = ("리바로", "리바로젯", "로수젯", "리피토")
FIXTURE_BRANDS = (*MATCHED_BRANDS, "악템라")
GENERIC_UNRESOLVED = (
    "어느 브랜드 기준인지 확인되지 않아 답변을 드릴 수 없습니다. "
    "브랜드명을 함께 알려주시거나, 시장 단위로 보시려면 시장을 지정해 주세요."
)


class _PlannerRaiseAgent:
    def answer(self, question: str, *args: object, **kwargs: object) -> dict:
        del args, kwargs
        _brand(question, MATCHED_BRANDS)
        raise AssertionError("brand unexpectedly resolved")


def _bq_message(question: str) -> str:
    resolver = BrandResolver(mode="fixture")
    grounding = build_period_grounding(question, current_month=lambda: "2026-06")
    schemas = tool_schemas(FIXTURE_BRANDS, grounding.schema_periods, default_catalog())
    plan = plan_bq_question(question, resolver, grounding, schemas)

    assert plan is not None
    assert type(plan).__name__ == "BqCardinalityStop"
    return plan.message


def _rendered_unresolved(message: str) -> str:
    return MarkdownResponseBuilder().brand_unresolved(message).markdown


def test_brand_unresolved_error_preserves_all_explicit_matches() -> None:
    with pytest.raises(BrandUnresolvedError) as excinfo:
        _brand(ED04_QUESTION, MATCHED_BRANDS)

    assert excinfo.value.matches == MATCHED_BRANDS


def test_ed04_reports_multi_brand_limit_with_actual_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service_app,
        "build_tool_use_agent",
        lambda _dependencies: _PlannerRaiseAgent(),
    )

    result = service_app._answer_direct_agent_loop(ED04_QUESTION, "live")

    assert result["answer"] == _rendered_unresolved(
        "리바로, 리바로젯, 로수젯, 리피토 중 한 브랜드를 지정해 다시 질문해 주세요. "
        "현재 이 분석 계약은 여러 브랜드를 한 번에 처리하지 않습니다."
    )


@pytest.mark.parametrize(
    ("question", "expected"),
    (
        (
            "리바로와 리바로젯 매출 알려줘",
            "리바로, 리바로젯 중 한 브랜드를 지정해 다시 질문해 주세요. "
            "현재 이 분석 계약은 여러 브랜드를 한 번에 처리하지 않습니다.",
        ),
        (
            "리바로와 리바로젯과 악템라 매출 알려줘",
            "리바로, 리바로젯, 악템라 중 한 브랜드를 지정해 다시 질문해 주세요. "
            "현재 이 분석 계약은 여러 브랜드를 한 번에 처리하지 않습니다.",
        ),
    ),
)
def test_existing_bq_cardinality_messages_are_byte_stable(
    question: str,
    expected: str,
) -> None:
    assert _bq_message(question).encode() == expected.encode()


def test_true_zero_match_keeps_existing_generic_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service_app,
        "build_tool_use_agent",
        lambda _dependencies: _PlannerRaiseAgent(),
    )

    result = service_app._answer_direct_agent_loop("고지혈증 전략뷰 HHI 알려줘", "live")

    assert result["answer"] == _rendered_unresolved(GENERIC_UNRESOLVED)
