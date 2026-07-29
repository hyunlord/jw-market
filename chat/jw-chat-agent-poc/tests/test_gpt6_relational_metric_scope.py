"""GPT6-FIX — HHI / market size / news / sales-activity requests must not inherit
a sales terminal relation summary.

Before this fix ``_requested_relational_metrics`` had no vocabulary for those four
subjects, so the request classified as empty (or fell back to ``generic``) and
``_ensure_terminal_relation_summary`` appended a sales direction sentence such as
"리바로 매출은 최근 2개월 연속 하락했습니다." to answers about market
concentration, market size, news and sales activity.
"""

from __future__ import annotations

import pytest

from jw_chat_agent_poc.service.answer_safety import (
    _requested_relational_metrics,
    _requested_series_metrics,
    enforce_relational_numeric_claims_with_trace,
)

_SALES_STREAK_MARK = "연속 하락"


def _falling_sales_call(brand: str = "리바로") -> dict[str, object]:
    """One successful sales series with a contiguous two-month fall."""

    return {
        "tool": "get_brand_sales",
        "status": "ok",
        "render_data": {
            "status": "ok",
            "brand": brand,
            "metric": "sales",
            "brand_value_series_10pt": [
                {"period": "2026-03", "value_억원": 120.0},
                {"period": "2026-04", "value_억원": 110.0},
                {"period": "2026-05", "value_억원": 100.0},
            ],
        },
    }


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("리바로가 속한 시장의 HHI 알려줘", "hhi"),
        ("리바로 시장 집중도 추이 어때?", "hhi"),
        ("이 시장의 허핀달 지수는?", "hhi"),
        ("리바로 시장규모 알려줘", "market_size"),
        ("리바로 시장 규모 변화 분석", "market_size"),
        ("리바로 관련 뉴스 알려줘", "news"),
        ("리바로 관련 기사 있어?", "news"),
        ("리바로 영업활동 알려줘", "activity"),
        ("리바로 활동량 추이", "activity"),
    ],
)
def test_new_subject_classifies_into_its_own_metric(question: str, expected: str) -> None:
    metrics = _requested_relational_metrics(question)
    assert expected in metrics


@pytest.mark.parametrize(
    "question",
    [
        "리바로 시장 집중도 추이 어때?",
        "리바로 시장 규모 변화 분석",
        "리바로 뉴스 변화 있어?",
        "리바로 영업활동 추이 어때?",
    ],
)
def test_new_subject_does_not_fall_back_to_generic(question: str) -> None:
    assert "generic" not in _requested_relational_metrics(question)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("리바로 점유율 알려줘", "share"),
        ("리바로 MS 알려줘", "share"),
        ("리바로 매출 추이 알려줘", "sales"),
        ("리바로 처방조제액 알려줘", "sales"),
        ("리바로 순위 알려줘", "rank"),
        ("리바로 랭킹 알려줘", "rank"),
        ("리바로 시장 대비 성장은?", "market"),
        ("리바로가 시장보다 빠르게 성장했어?", "market"),
    ],
)
def test_existing_metric_vocabulary_is_preserved(question: str, expected: str) -> None:
    assert expected in _requested_relational_metrics(question)


@pytest.mark.parametrize(
    ("question", "seed"),
    [
        ("리바로가 속한 시장의 HHI 알려줘", "리바로가 속한 시장의 HHI는 1,850입니다."),
        ("리바로 시장 집중도 추이 어때?", "해당 시장의 시장 집중도는 1,850 수준입니다."),
        ("리바로 시장규모 알려줘", "해당 시장의 시장규모는 4,200억원입니다."),
        ("리바로 관련 뉴스 알려줘", "리바로 관련 최신 기사는 3건 확인됩니다."),
        ("리바로 영업활동 알려줘", "리바로 영업활동 지표는 다음과 같습니다."),
    ],
)
def test_sales_terminal_summary_is_not_injected_into_other_subjects(question: str, seed: str) -> None:
    result = enforce_relational_numeric_claims_with_trace(question, seed, [_falling_sales_call()])
    assert _SALES_STREAK_MARK not in result.answer
    assert result.answer.strip() == seed


@pytest.mark.parametrize(
    ("question", "seed"),
    [
        ("리바로 시장규모와 매출을 같이 알려줘", "리바로 시장규모는 4,200억원이고 매출은 100억원입니다."),
        ("리바로 시장 HHI와 매출 추이 알려줘", "HHI는 1,850이고 리바로 매출은 100억원입니다."),
        ("리바로 매출 추이 알려줘", "리바로 매출은 100억원입니다."),
    ],
)
def test_terminal_summary_survives_when_request_crosses_sales(question: str, seed: str) -> None:
    """Suppression is conditional on crossing, not unconditional."""

    result = enforce_relational_numeric_claims_with_trace(question, seed, [_falling_sales_call()])
    assert _SALES_STREAK_MARK in result.answer


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("리바로가 속한 시장의 HHI 알려줘", frozenset()),
        ("리바로 시장 집중도 추이 어때?", frozenset()),
        ("리바로 관련 뉴스 알려줘", frozenset()),
        ("리바로 영업활동 알려줘", frozenset()),
        ("리바로 매출 추이 알려줘", frozenset({"sales"})),
        ("리바로 점유율 알려줘", frozenset({"share"})),
        ("리바로 어때?", frozenset({"generic"})),
        ("리바로 시장규모와 매출을 같이 알려줘", frozenset({"sales"})),
    ],
)
def test_series_view_hides_new_categories_from_legacy_gates(
    question: str, expected: frozenset[str]
) -> None:
    """The relational-series gates keep consuming only series-bearing metrics."""

    assert _requested_series_metrics(question) == expected
