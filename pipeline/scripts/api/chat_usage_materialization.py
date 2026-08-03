from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final, Literal

CHAT_MATERIALIZATION_MAX_AGE: Final = timedelta(minutes=15)

CHAT_USAGE_SQL: Final[dict[str, str]] = {
    "chat_trend": """
        WITH daily AS (
            SELECT {period} AS period, d.service_id,
                   CAST(SUM(d.turns) AS UNSIGNED) AS turns,
                   SUM(d.attributed_turns) AS attributed_turns,
                   SUM(d.total_tokens) AS total_tokens
            FROM jw_mart.mart_chat_usage_daily d
            LEFT JOIN dashboard_user_directory_v u ON u.id=d.portal_user_id
            WHERE d.usage_date >= %s AND d.usage_date < %s
            {user_filter}
            GROUP BY period, d.service_id
        ), sessions AS (
            SELECT {period} AS period, s.service_id,
                   COUNT(DISTINCT s.conversation_id) AS sessions
            FROM jw_mart.mart_chat_usage_daily_session s
            LEFT JOIN dashboard_user_directory_v u ON u.id=s.portal_user_id
            WHERE s.usage_date >= %s AND s.usage_date < %s
            {user_filter}
            GROUP BY period, s.service_id
        )
        SELECT d.period, d.service_id, d.turns,
               COALESCE(s.sessions, 0) AS sessions,
               d.attributed_turns, d.total_tokens
        FROM daily d
        LEFT JOIN sessions s
          ON s.period=d.period AND s.service_id <=> d.service_id
        ORDER BY d.period, d.service_id
    """,
    "chat_user": """
        WITH daily AS (
            SELECT d.portal_user_id, CAST(SUM(d.turns) AS UNSIGNED) AS turns,
                   SUM(d.total_tokens) AS total_tokens
            FROM jw_mart.mart_chat_usage_daily d
            WHERE d.usage_date >= %s AND d.usage_date < %s
              AND d.portal_user_id IS NOT NULL
            GROUP BY d.portal_user_id
        ), sessions AS (
            SELECT s.portal_user_id,
                   COUNT(DISTINCT s.conversation_id) AS sessions
            FROM jw_mart.mart_chat_usage_daily_session s
            WHERE s.usage_date >= %s AND s.usage_date < %s
              AND s.portal_user_id IS NOT NULL
            GROUP BY s.portal_user_id
        )
        SELECT u.id AS user_id, u.name AS user_name, u.department,
               d.turns, COALESCE(s.sessions, 0) AS sessions, d.total_tokens
        FROM daily d
        JOIN dashboard_user_directory_v u ON u.id=d.portal_user_id
        LEFT JOIN sessions s ON s.portal_user_id=d.portal_user_id
        WHERE 1=1 {user_filter}
        ORDER BY d.turns DESC, u.id LIMIT 100
    """,
    "chat_user_service": """
        WITH daily AS (
            SELECT d.portal_user_id, d.service_id,
                   CAST(SUM(d.turns) AS UNSIGNED) AS turns,
                   SUM(d.total_tokens) AS total_tokens
            FROM jw_mart.mart_chat_usage_daily d
            WHERE d.usage_date >= %s AND d.usage_date < %s
              AND d.portal_user_id IS NOT NULL
            GROUP BY d.portal_user_id, d.service_id
        ), sessions AS (
            SELECT s.portal_user_id, s.service_id,
                   COUNT(DISTINCT s.conversation_id) AS sessions
            FROM jw_mart.mart_chat_usage_daily_session s
            WHERE s.usage_date >= %s AND s.usage_date < %s
              AND s.portal_user_id IS NOT NULL
            GROUP BY s.portal_user_id, s.service_id
        )
        SELECT u.id AS user_id, u.name AS user_name, u.department,
               d.service_id, d.turns, COALESCE(s.sessions, 0) AS sessions,
               d.total_tokens
        FROM daily d
        JOIN dashboard_user_directory_v u ON u.id=d.portal_user_id
        LEFT JOIN sessions s
          ON s.portal_user_id=d.portal_user_id
         AND s.service_id <=> d.service_id
        WHERE 1=1 {user_filter}
        ORDER BY d.turns DESC, u.id, d.service_id LIMIT 200
    """,
}

CHAT_MATERIALIZATION_STATE_SQL: Final = """
    SELECT coverage_start, coverage_end_exclusive, last_success_at, status
    FROM jw_mart.mart_chat_usage_refresh_state
    WHERE singleton_key=1
"""


class ChatMaterializationUnavailable(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        reason: Literal["missing", "status", "stale", "coverage"],
        state: ChatMaterializationState | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.available_from = state.coverage_start if state is not None else None
        self.available_to = (
            state.coverage_end_exclusive - timedelta(days=1) if state is not None else None
        )


@dataclass(frozen=True, slots=True)
class ChatMaterializationState:
    coverage_start: date
    coverage_end_exclusive: date
    last_success_at: datetime | None
    status: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ChatMaterializationState:
        last_success_at = row["last_success_at"]
        if last_success_at is not None and last_success_at.tzinfo is None:
            last_success_at = last_success_at.replace(tzinfo=UTC)
        return cls(
            coverage_start=row["coverage_start"],
            coverage_end_exclusive=row["coverage_end_exclusive"],
            last_success_at=last_success_at,
            status=str(row["status"]),
        )


def validate_materialization_state(
    state: ChatMaterializationState | None,
    filters: Any,
    *,
    now: datetime | None = None,
) -> None:
    if state is None:
        raise ChatMaterializationUnavailable(
            "chat materialization state is missing",
            reason="missing",
        )
    if state.status != "complete":
        raise ChatMaterializationUnavailable(
            f"chat materialization status is not complete: {state.status}",
            reason="status",
            state=state,
        )
    current = now or datetime.now(UTC)
    if (
        state.last_success_at is None
        or current - state.last_success_at > CHAT_MATERIALIZATION_MAX_AGE
    ):
        raise ChatMaterializationUnavailable(
            "chat materialization is stale",
            reason="stale",
            state=state,
        )
    requested_end = filters.date_to + timedelta(days=1)
    if (
        state.coverage_start > filters.date_from
        or state.coverage_end_exclusive < requested_end
    ):
        raise ChatMaterializationUnavailable(
            "chat materialization coverage is incomplete",
            reason="coverage",
            state=state,
        )
