from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from pipeline.scripts.api.chat_usage_materialization import (
    CHAT_USAGE_SQL,
    ChatMaterializationState,
    ChatMaterializationUnavailable,
    validate_materialization_state,
)
from pipeline.scripts.api.dashboard_usage import UsageFilters


def test_chat_queries_use_daily_facts_and_exact_session_bridge() -> None:
    sql = "\n".join(CHAT_USAGE_SQL.values()).lower()

    assert "jw_mart.mart_chat_usage_daily" in sql
    assert "jw_mart.mart_chat_usage_daily_session" in sql
    assert "row_number() over" in sql
    assert "count(distinct s.conversation_id)" not in sql
    assert "dashboard_chat_usage_v" not in sql
    assert all(
        "CAST(SUM(d.turns) AS UNSIGNED)" in query
        for query in CHAT_USAGE_SQL.values()
    )
    assert "trace_json" not in sql


def test_chat_session_trends_count_each_conversation_once_across_dates_and_services() -> None:
    fixture = [(f"session-{index}", "2026-07-11", 91) for index in range(536)]
    fixture.extend((f"session-{index}", "2026-07-12", 91) for index in range(8))
    grouped_distinct = len({(conversation, day, service) for conversation, day, service in fixture})
    period_distinct = len({conversation for conversation, _day, _service in fixture})

    assert grouped_distinct == 544
    assert period_distinct == 536
    cross_service_fixture = [
        ("shared-session", "2026-07-11", 61),
        ("shared-session", "2026-07-11", 91),
    ]
    assert len({row[0] for row in cross_service_fixture}) == 1
    assert len({(row[1], row[2]) for row in cross_service_fixture}) == 2
    for query_name in ("chat_trend", "chat_user", "chat_user_service"):
        normalized = " ".join(CHAT_USAGE_SQL[query_name].lower().split())
        assert "row_number() over ( partition by s.conversation_id" in normalized
        assert "session_rank = 1" in normalized


def test_materialization_accepts_fresh_complete_coverage() -> None:
    now = datetime(2026, 8, 3, 1, 0, tzinfo=UTC)
    state = ChatMaterializationState(
        coverage_start=date(2026, 7, 1),
        coverage_end_exclusive=date(2026, 8, 4),
        last_success_at=now - timedelta(minutes=4),
        status="complete",
    )

    validate_materialization_state(
        state,
        UsageFilters(date(2026, 7, 3), date(2026, 8, 3), "day"),
        now=now,
    )


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (None, "missing"),
        (
            ChatMaterializationState(
                date(2026, 7, 1),
                date(2026, 8, 4),
                datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
                "failed",
            ),
            "status",
        ),
        (
            ChatMaterializationState(
                date(2026, 7, 1),
                date(2026, 8, 4),
                datetime(2026, 8, 3, 0, 30, tzinfo=UTC),
                "complete",
            ),
            "stale",
        ),
        (
            ChatMaterializationState(
                date(2026, 7, 10),
                date(2026, 8, 4),
                datetime(2026, 8, 3, 0, 59, tzinfo=UTC),
                "complete",
            ),
            "coverage",
        ),
    ],
)
def test_materialization_fails_closed_for_missing_failed_stale_or_partial_state(
    state: ChatMaterializationState | None,
    message: str,
) -> None:
    with pytest.raises(ChatMaterializationUnavailable, match=message):
        validate_materialization_state(
            state,
            UsageFilters(date(2026, 7, 3), date(2026, 8, 3), "day"),
            now=datetime(2026, 8, 3, 1, 0, tzinfo=UTC),
        )


def test_coverage_failure_preserves_available_range_for_http_mapping() -> None:
    state = ChatMaterializationState(
        date(2026, 7, 9),
        date(2026, 8, 4),
        datetime(2026, 8, 4, 0, 1, tzinfo=UTC),
        "complete",
    )

    with pytest.raises(ChatMaterializationUnavailable) as caught:
        validate_materialization_state(
            state,
            UsageFilters(date(2026, 7, 8), date(2026, 8, 3), "day"),
            now=datetime(2026, 8, 4, 0, 5, tzinfo=UTC),
        )

    assert caught.value.reason == "coverage"
    assert caught.value.available_from == date(2026, 7, 9)
    assert caught.value.available_to == date(2026, 8, 3)
