"""GPT6-GENERIC-RESTORE — requests that name no competing metric keep their summary.

GPT6-FIX narrowed the terminal relation summary to requests that explicitly cross
the metric the summary states, which also dropped the summary for bare trend
requests ("리바로 어때?" -> {"generic"}) and for requests naming no metric at all
("리바로에 대해 알려줘" -> empty set). Both are restored here: "generic" is listed
in _TERMINAL_SUMMARY_METRICS, and the empty set is handled by its own branch in
_terminal_summary_metric_requested, since intersecting an empty set never matches.

The four subjects GPT6-FIX introduced - hhi, market_size, news, activity - stay
suppressed. Those cases are asserted here as well so the revert cannot creep.
"""

from __future__ import annotations

import pytest

from jw_chat_agent_poc.service.answer_safety import (
    _TERMINAL_SUMMARY_METRICS,
    _requested_relational_metrics,
    _terminal_summary_metric_requested,
    enforce_relational_numeric_claims_with_trace,
)

_SALES_STREAK_MARK = "연속 하락"


def _falling_sales_call(brand: str = "리바로") -> dict[str, object]:
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


def test_generic_is_eligible_for_the_terminal_summary() -> None:
    assert "generic" in _TERMINAL_SUMMARY_METRICS


def test_new_subjects_are_not_eligible() -> None:
    """The revert covers "generic" only."""

    assert not _TERMINAL_SUMMARY_METRICS & {"hhi", "market_size", "news", "activity"}


@pytest.mark.parametrize(
    "question",
    ["리바로 어때?", "리바로 추이는?", "리바로 변화 있어?", "리바로 흐름 알려줘", "리바로 성장 어때?"],
)
def test_generic_request_is_summary_eligible(question: str) -> None:
    assert _requested_relational_metrics(question) == frozenset({"generic"})
    assert _terminal_summary_metric_requested(question) is True


@pytest.mark.parametrize(
    ("question", "seed"),
    [
        ("리바로 어때?", "리바로 정보입니다."),
        ("리바로 추이는?", "리바로 정보입니다."),
        ("리바로 변화 있어?", "리바로 정보입니다."),
        ("리바로 흐름 알려줘", "리바로 정보입니다."),
    ],
)
def test_generic_request_keeps_the_sales_summary(question: str, seed: str) -> None:
    result = enforce_relational_numeric_claims_with_trace(question, seed, [_falling_sales_call()])
    assert _SALES_STREAK_MARK in result.answer


@pytest.mark.parametrize(
    ("question", "seed"),
    [
        ("리바로가 속한 시장의 HHI 알려줘", "HHI는 1,850입니다."),
        ("리바로 시장 집중도 추이 어때?", "시장 집중도는 1,850 수준입니다."),
        ("리바로 시장규모 알려줘", "시장규모는 4,200억원입니다."),
        ("리바로 시장 규모 변화 분석", "시장규모는 4,200억원입니다."),
        ("리바로 관련 뉴스 알려줘", "최신 기사는 3건입니다."),
        ("리바로 관련 이슈 변화 있어?", "최신 기사는 3건입니다."),
        ("리바로 영업활동 알려줘", "영업활동 지표는 다음과 같습니다."),
        ("리바로 영업활동 추이 어때?", "영업활동 지표는 다음과 같습니다."),
    ],
)
def test_new_subjects_stay_suppressed_after_the_revert(question: str, seed: str) -> None:
    """GPT6-FIX's result must survive this revert."""

    assert not _terminal_summary_metric_requested(question)
    result = enforce_relational_numeric_claims_with_trace(question, seed, [_falling_sales_call()])
    assert _SALES_STREAK_MARK not in result.answer
    assert result.answer.strip() == seed


@pytest.mark.parametrize(
    ("question", "seed"),
    [
        ("리바로 시장규모와 매출을 같이 알려줘", "시장규모 4,200억원, 매출 100억원입니다."),
        ("리바로 매출 추이 알려줘", "매출은 100억원입니다."),
    ],
)
def test_crossing_requests_are_unaffected(question: str, seed: str) -> None:
    result = enforce_relational_numeric_claims_with_trace(question, seed, [_falling_sales_call()])
    assert _SALES_STREAK_MARK in result.answer


@pytest.mark.parametrize(
    "question",
    ["리바로에 대해 알려줘", "리바로 정보 줘", "리바로"],
)
def test_requests_expressing_no_metric_intent_are_eligible(question: str) -> None:
    """A request naming no metric classifies to the empty set, not to "generic".

    ``frozenset() & _TERMINAL_SUMMARY_METRICS`` is empty whatever the constant
    holds, so eligibility for these is carried by the empty-request branch rather
    than by the constant.
    """

    assert _requested_relational_metrics(question) == frozenset()
    assert _terminal_summary_metric_requested(question) is True


@pytest.mark.parametrize(
    ("question", "seed"),
    [
        ("리바로에 대해 알려줘", "리바로 정보입니다."),
        ("리바로 정보 줘", "리바로 정보입니다."),
    ],
)
def test_requests_expressing_no_metric_intent_keep_the_sales_summary(question: str, seed: str) -> None:
    result = enforce_relational_numeric_claims_with_trace(question, seed, [_falling_sales_call()])
    assert _SALES_STREAK_MARK in result.answer
