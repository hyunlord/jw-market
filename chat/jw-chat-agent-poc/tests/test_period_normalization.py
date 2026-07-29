from __future__ import annotations

import pytest

from jw_chat_agent_poc.agentic.sales_filter_extraction import extract_metric_filter_entries
from jw_chat_agent_poc.common.periods import (
    canonical_periods,
    has_explicit_period_cue,
    month_keys,
    requested_period,
)
from jw_chat_agent_poc.service.app import SessionStore, _answer_existing_without_pending


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("리바로 2025년 4월 매출", ("2025-04",)),
        ("리바로 2025-04 매출", ("2025-04",)),
        ("리바로 2025/04 매출", ("2025-04",)),
        ("리바로 25년 4월 매출", ("2025-04",)),
        ("리바로 2025년 4분기 매출", ("2025-Q4",)),
        ("리바로 2025-Q4 매출", ("2025-Q4",)),
    ],
)
def test_canonical_periods_normalize_user_facing_periods(
    text: str,
    expected: tuple[str, ...],
) -> None:
    assert canonical_periods(text) == expected


def test_month_keys_share_the_canonical_period_parser() -> None:
    assert month_keys("VALUES LC SI PRICE 1/2026") == frozenset({"2026-01"})
    assert month_keys("2026년 1월 총 sell-out 금액") == frozenset({"2026-01"})


def test_metric_filters_use_canonical_month_and_quarter_periods() -> None:
    assert ("period_month", "2025-04") in extract_metric_filter_entries(
        "리바로 2025년 4월 매출"
    )
    assert ("period_month", "2025-Q4") in extract_metric_filter_entries(
        "리바로 2025년 4분기 매출"
    )


def test_unrecognized_explicit_period_is_not_silently_dropped() -> None:
    filters = extract_metric_filter_entries("리바로 2025년 13월 매출")

    assert has_explicit_period_cue("리바로 2025년 13월 매출")
    assert ("period_month", "2025년 13월") in filters


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("리바로 2024년 매출", "2024"),
        ("리바로 최근 3년 추이", "최근 3년"),
        ("리바로 2025년 2분기 매출", "2025-Q2"),
        ("고지혈증 시장에 어떤 브랜드들이 있어?", None),
    ),
)
def test_requested_period_preserves_year_relative_and_quarter_constraints(
    text: str,
    expected: str | None,
) -> None:
    assert requested_period(text) == expected


class _RecordingMarketResolver:
    def __init__(self) -> None:
        self.periods: list[str] = []

    def answer_market_id(
        self,
        _question: str,
        *,
        market_id: str,
        period: str,
    ) -> dict[str, str]:
        self.periods.append(period)
        return {"market_id": market_id, "period": period}


@pytest.mark.parametrize(
    ("question", "expected_period"),
    (
        ("ml_006 2025년 4월 시장규모", "2025-04"),
        ("ml_006 2025-04 시장규모", "2025-04"),
        ("ml_006 2025년 4분기 시장규모", "2025-Q4"),
        ("ml_006 2024년 시장규모", "2024"),
        ("ml_006 최근 3년 시장 추이", "최근 3년"),
        ("ml_006 2025년 13월 시장규모", "2025년 13월"),
    ),
)
def test_explicit_market_route_preserves_requested_period(
    question: str,
    expected_period: str,
) -> None:
    resolver = _RecordingMarketResolver()

    result = _answer_existing_without_pending(
        resolver,
        lambda **_kwargs: pytest.fail("agent fallback must not run"),
        "period-normalization-test",
        question,
        "live",
        None,
        SessionStore(),
    )

    assert result["period"] == expected_period
    assert resolver.periods == [expected_period]


@pytest.mark.parametrize(
    ("question", "expected_months"),
    (
        # 개년 is the form that used to match nothing and fall back to the latest point.
        ("최근 3개년", 36),
        ("최근 1개년", 12),
        ("최근 5개년", 60),
        # the units that already worked must be untouched
        ("최근 3년", 36),
        ("최근 1년", 12),
        ("최근 5년", 60),
        ("최근 6개월", 6),
        ("최근 12개월", 12),
        ("최근 3달", 3),
        # the 2..60 clamp applies to the new unit exactly as it does to 년
        ("최근 6개년", None),
        ("최근 6년", None),
        ("최근 61개월", None),
        ("최근 1개월", None),
        # 최근 and the digits are both still required
        ("최근 개년", None),
        ("3개년", None),
        ("최근 3분기", None),
    ),
)
def test_relative_history_points_reads_every_year_and_month_wording(
    question: str,
    expected_months: int | None,
) -> None:
    from jw_chat_agent_poc.agent_loop.bq_planner import (
        _relative_history_points as bq_planner_points,
    )
    from jw_chat_agent_poc.agent_loop.structured_planner import (
        _relative_history_points as structured_points,
    )

    # The two planners carry the same parser; they must never disagree.
    assert structured_points(question) == expected_months
    assert bq_planner_points(question) == expected_months
