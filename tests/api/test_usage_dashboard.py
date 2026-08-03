from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import pymysql
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pipeline.scripts.api.config import APIConfig, config
from pipeline.scripts.api.dashboard_usage import (
    DashboardCache,
    DashboardQuery,
    DashboardQueryError,
    MariaDBUsageRepository,
    UsageFilters,
    UsageStatsService,
)
from pipeline.scripts.api.chat_usage_materialization import (
    ChatMaterializationState,
    ChatMaterializationUnavailable,
    validate_materialization_state,
)
from pipeline.scripts.api.routes.dashboard_usage import create_usage_dashboard_router


class FakeRepository:
    def __init__(self) -> None:
        self.calls: list[UsageFilters] = []

    def fetch(self, filters: UsageFilters) -> dict:
        self.calls.append(filters)
        return {
            "api": {
                "trend": [],
                "by_endpoint": [],
                "by_user": [],
                "by_department": [],
                "by_status": [],
                "by_weekday_hour": [],
            },
            "chat": {"trend": [], "by_user": [], "by_user_service": []},
            "auth": {"trend": [], "by_type": [], "by_hour": [], "audience": []},
            "credit": {"trend": [], "by_user": [], "by_department": []},
            "reports": {"trend": [], "by_type": []},
            "filter_options": {"users": [], "departments": []},
        }


class CoverageRepository(FakeRepository):
    def __init__(self, state: ChatMaterializationState | None) -> None:
        super().__init__()
        self.state = state

    def fetch(self, filters: UsageFilters) -> dict:
        self.calls.append(filters)
        validate_materialization_state(
            self.state,
            filters,
            now=datetime(2026, 8, 4, 0, 5, tzinfo=UTC),
        )
        result = super().fetch(filters)
        self.calls.pop()
        return result


def _dashboard_config() -> APIConfig:
    return replace(
        config,
        dashboard_db_host="db.internal",
        dashboard_db_user="dashboard_reader",
        dashboard_db_password="test-only",
        dashboard_db_name="llmops",
    )


class DeterministicUsageRepository(MariaDBUsageRepository):
    def __init__(self, *, max_workers: int, failing_query: str | None = None) -> None:
        super().__init__(_dashboard_config(), max_workers=max_workers)
        self.failing_query = failing_query
        self.active_queries = 0
        self.max_active_queries = 0
        self.completed_queries: list[str] = []
        self._state_lock = threading.Lock()

    def _execute_query(self, query: DashboardQuery) -> tuple[str, list[dict[str, Any]]]:
        with self._state_lock:
            self.active_queries += 1
            self.max_active_queries = max(self.max_active_queries, self.active_queries)
        try:
            time.sleep(0.005)
            if query.name == self.failing_query:
                raise pymysql.OperationalError(1040, "Too many connections")
            if query.name == "filter_options":
                rows = [{"user_id": 34, "user_name": "tester", "department": "QA"}]
            elif query.name == "auth_audience":
                rows = [{"new_users": 2, "returning_users": 3}]
            else:
                rows = [{"query": query.name, "ordinal": query.ordinal}]
            with self._state_lock:
                self.completed_queries.append(query.name)
            return query.name, rows
        finally:
            with self._state_lock:
                self.active_queries -= 1

    def _fetch_chat_materialization_state(self) -> ChatMaterializationState:
        return ChatMaterializationState(
            coverage_start=date(2020, 1, 1),
            coverage_end_exclusive=date(2030, 1, 1),
            last_success_at=datetime.now(UTC),
            status="complete",
        )


def _client(repository: FakeRepository) -> TestClient:
    app = FastAPI()
    service = UsageStatsService(repository, cache=DashboardCache(ttl_seconds=60))
    app.include_router(create_usage_dashboard_router(service))
    return TestClient(app)


def test_usage_dashboard_defaults_to_thirty_days_and_daily_grain(monkeypatch) -> None:
    repository = FakeRepository()
    monkeypatch.setattr("pipeline.scripts.api.routes.dashboard_usage._today", lambda: date(2026, 8, 2))

    response = _client(repository).get("/api/dashboard/usage-stats")

    assert response.status_code == 200
    assert repository.calls == [
        UsageFilters(date_from=date(2026, 7, 4), date_to=date(2026, 8, 2), grain="day")
    ]
    assert response.json()["limits"] == {"max_days": 366, "cache_ttl_seconds": 60}


def test_usage_dashboard_clamps_omitted_defaults_to_fresh_coverage(monkeypatch) -> None:
    repository = CoverageRepository(
        ChatMaterializationState(
            coverage_start=date(2026, 7, 9),
            coverage_end_exclusive=date(2026, 8, 4),
            last_success_at=datetime(2026, 8, 4, 0, 1, tzinfo=UTC),
            status="complete",
        )
    )
    monkeypatch.setattr(
        "pipeline.scripts.api.routes.dashboard_usage._today", lambda: date(2026, 8, 4)
    )

    response = _client(repository).get("/api/dashboard/usage-stats")

    assert response.status_code == 200
    assert response.json()["filters"] == {
        "date_from": "2026-07-09",
        "date_to": "2026-08-03",
        "grain": "day",
        "user_id": None,
        "department": None,
    }
    assert repository.calls == [
        UsageFilters(date(2026, 7, 6), date(2026, 8, 4), "day"),
        UsageFilters(date(2026, 7, 9), date(2026, 8, 3), "day"),
    ]


@pytest.mark.parametrize(
    ("query", "expected_from", "expected_to"),
    [
        ("date_from=2026-07-09", "2026-07-09", "2026-08-03"),
        ("date_to=2026-08-03", "2026-07-09", "2026-08-03"),
    ],
)
def test_usage_dashboard_clamps_only_omitted_coverage_bound(
    monkeypatch,
    query: str,
    expected_from: str,
    expected_to: str,
) -> None:
    repository = CoverageRepository(
        ChatMaterializationState(
            coverage_start=date(2026, 7, 9),
            coverage_end_exclusive=date(2026, 8, 4),
            last_success_at=datetime(2026, 8, 4, 0, 1, tzinfo=UTC),
            status="complete",
        )
    )
    monkeypatch.setattr(
        "pipeline.scripts.api.routes.dashboard_usage._today", lambda: date(2026, 8, 4)
    )

    response = _client(repository).get(f"/api/dashboard/usage-stats?{query}")

    assert response.status_code == 200
    assert response.json()["filters"]["date_from"] == expected_from
    assert response.json()["filters"]["date_to"] == expected_to


@pytest.mark.parametrize(
    ("date_from", "date_to", "expected_status"),
    [
        ("2026-07-08", "2026-08-03", 503),
        ("2026-07-09", "2026-08-03", 200),
        ("2026-07-09", "2026-08-04", 503),
    ],
)
def test_usage_dashboard_preserves_explicit_coverage_boundaries(
    date_from: str,
    date_to: str,
    expected_status: int,
) -> None:
    repository = CoverageRepository(
        ChatMaterializationState(
            date(2026, 7, 9),
            date(2026, 8, 4),
            datetime(2026, 8, 4, 0, 1, tzinfo=UTC),
            "complete",
        )
    )

    response = _client(repository).get(
        f"/api/dashboard/usage-stats?date_from={date_from}&date_to={date_to}"
    )

    assert response.status_code == expected_status
    if expected_status == 503:
        assert response.json() == {
            "detail": {
                "error": "chat_materialization_unavailable",
                "reason": "coverage",
                "message": "요청한 기간의 채팅 통계가 아직 준비되지 않았습니다.",
                "available_from": "2026-07-09",
                "available_to": "2026-08-03",
            }
        }


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (None, "missing"),
        (
            ChatMaterializationState(
                date(2026, 7, 9),
                date(2026, 8, 4),
                datetime(2026, 8, 4, 0, 1, tzinfo=UTC) - timedelta(minutes=16),
                "complete",
            ),
            "stale",
        ),
    ],
)
def test_usage_dashboard_maps_unavailable_materialization_to_json_503(
    state: ChatMaterializationState | None,
    reason: str,
) -> None:
    response = _client(CoverageRepository(state)).get(
        "/api/dashboard/usage-stats?date_from=2026-07-09&date_to=2026-08-03"
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["error"] == "chat_materialization_unavailable"
    assert detail["reason"] == reason
    assert isinstance(detail["message"], str)


def test_usage_dashboard_maps_query_failure_to_json_503() -> None:
    class FailingRepository(FakeRepository):
        def fetch(self, filters: UsageFilters) -> dict:
            raise DashboardQueryError("chat_trend")

    response = _client(FailingRepository()).get(
        "/api/dashboard/usage-stats?date_from=2026-07-09&date_to=2026-08-03"
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "error": "dashboard_query_unavailable",
            "message": "사용 통계 데이터 소스를 조회할 수 없습니다. 잠시 후 다시 시도해 주세요.",
        }
    }


def test_usage_dashboard_rejects_an_inverted_or_oversized_range() -> None:
    client = _client(FakeRepository())

    inverted = client.get("/api/dashboard/usage-stats?date_from=2026-08-02&date_to=2026-08-01")
    oversized = client.get("/api/dashboard/usage-stats?date_from=2025-01-01&date_to=2026-08-02")

    assert inverted.status_code == 422
    assert oversized.status_code == 422
    assert oversized.json()["detail"] == "조회 기간은 최대 366일입니다."


def test_usage_dashboard_forwards_filters_and_uses_cache() -> None:
    repository = FakeRepository()
    client = _client(repository)
    url = (
        "/api/dashboard/usage-stats?date_from=2026-07-01&date_to=2026-07-31"
        "&grain=week&user_id=34&department=JW%EC%A4%91%EC%99%B8%EC%A0%9C%EC%95%BD"
    )

    first = client.get(url)
    second = client.get(url)

    assert first.status_code == second.status_code == 200
    assert len(repository.calls) == 1
    assert repository.calls[0].user_id == 34
    assert repository.calls[0].department == "JW중외제약"


def test_service_category_is_explicit_and_unknown_is_not_relabelled() -> None:
    assert UsageStatsService.service_category(61) == "rnd"
    assert UsageStatsService.service_category(91) == "market"
    assert UsageStatsService.service_category(94) == "market"
    assert UsageStatsService.service_category(999) == "unknown"
    assert UsageStatsService.service_category(None) == "unknown"


def test_service_adds_categories_to_chat_user_rows_without_rewriting_unknown() -> None:
    class CategorizedRepository(FakeRepository):
        def fetch(self, filters: UsageFilters) -> dict:
            result = super().fetch(filters)
            result["chat"]["by_user_service"] = [
                {"user_id": 1, "service_id": 61, "turns": 4, "sessions": 2},
                {"user_id": 2, "service_id": None, "turns": 1, "sessions": 1},
            ]
            return result

    payload = UsageStatsService(
        CategorizedRepository(), cache=DashboardCache(ttl_seconds=60)
    ).get(UsageFilters(date(2026, 7, 1), date(2026, 7, 31), "day"))

    assert [row["service_category"] for row in payload["chat"]["by_user_service"]] == [
        "rnd",
        "unknown",
    ]


def test_service_reports_chat_service_linkage_quality_without_rewriting_trend() -> None:
    class ChatQualityRepository(FakeRepository):
        def fetch(self, filters: UsageFilters) -> dict:
            result = super().fetch(filters)
            result["chat"]["trend"] = [
                {
                    "period": "2026-07-01",
                    "service_id": 61,
                    "turns": 4,
                    "sessions": 2,
                    "attributed_turns": 3,
                    "total_tokens": 40,
                },
                {
                    "period": "2026-07-01",
                    "service_id": None,
                    "turns": 6,
                    "sessions": 3,
                    "attributed_turns": 1,
                    "total_tokens": 60,
                },
                {
                    "period": "2026-07-02",
                    "service_id": 94,
                    "turns": 6,
                    "sessions": 2,
                    "attributed_turns": 6,
                    "total_tokens": 80,
                },
            ]
            return result

    payload = UsageStatsService(
        ChatQualityRepository(), cache=DashboardCache(ttl_seconds=60)
    ).get(UsageFilters(date(2026, 7, 1), date(2026, 7, 31), "day"))

    assert payload["data_quality"]["chat_service_linked_turns"] == 10
    assert payload["data_quality"]["chat_service_linkage_missing_turns"] == 6
    assert payload["chat"]["trend"][1]["service_id"] is None
    assert payload["chat"]["trend"][1]["turns"] == 6


def test_service_adds_chat_service_share_excluding_unknown_from_denominator() -> None:
    class ChatShareRepository(FakeRepository):
        def fetch(self, filters: UsageFilters) -> dict:
            result = super().fetch(filters)
            result["chat"]["trend"] = [
                {"period": "2026-07-01", "service_id": 61, "turns": 2},
                {"period": "2026-07-01", "service_id": 91, "turns": 3},
                {"period": "2026-07-01", "service_id": None, "turns": 95},
                {"period": "2026-07-02", "service_id": 999, "turns": 5},
            ]
            return result

    payload = UsageStatsService(
        ChatShareRepository(), cache=DashboardCache(ttl_seconds=60)
    ).get(UsageFilters(date(2026, 7, 1), date(2026, 7, 31), "day"))

    assert payload["chat"]["service_share"] == [
        {"service_category": "market", "turns": 3, "share": 0.6},
        {"service_category": "rnd", "turns": 2, "share": 0.4},
    ]
    assert [row["turns"] for row in payload["chat"]["trend"]] == [2, 3, 95, 5]


def test_dashboard_sql_exposes_supported_multidimensional_statistics() -> None:
    from pipeline.scripts.api.dashboard_usage import DASHBOARD_SQL

    assert {
        "api_status",
        "api_weekday_hour",
        "chat_user_service",
        "auth_hour",
        "auth_audience",
        "credit_department",
    }.issubset(DASHBOARD_SQL)
    assert "http_status" in DASHBOARD_SQL["api_status"]
    assert "HOUR(" in DASHBOARD_SQL["api_weekday_hour"]
    assert "service_id" in DASHBOARD_SQL["chat_user_service"]
    assert "AT0001" in DASHBOARD_SQL["auth_audience"]


def test_auth_audience_deduplicates_active_users_before_history_join() -> None:
    from pipeline.scripts.api.dashboard_usage import DASHBOARD_SQL

    sql = " ".join(DASHBOARD_SQL["auth_audience"].split())

    assert "SELECT DISTINCT a.user_id" in sql
    assert ") active ON active.user_id=history.user_id" in sql
    assert "WHERE history.type_code='AT0001'" in sql


def test_repository_queries_are_sanitized_view_only() -> None:
    from pipeline.scripts.api.dashboard_usage import DASHBOARD_SQL

    forbidden = {
        "audit_api_call_log",
        "jw_chat_agent_conversation_log",
        "question_text",
        "answer_text",
        "credit_update_history",
        "user_auth_log_tb",
    }
    sql = "\n".join(DASHBOARD_SQL.values()).lower()

    assert "dashboard_api_usage_v" in sql
    assert "jw_mart.mart_chat_usage_daily" in sql
    assert "jw_mart.mart_chat_usage_daily_session" in sql
    assert "dashboard_auth_event_v" in sql
    assert "dashboard_credit_usage_v" in sql
    assert "dashboard_report_download_v" in sql
    assert "dashboard_user_directory_v" in sql
    assert not forbidden.intersection(sql.split())


def test_repository_fails_closed_when_reader_settings_are_partial() -> None:
    with pytest.raises(
        ValueError,
        match="DASHBOARD_DB_USER, DASHBOARD_DB_PASSWORD, DASHBOARD_DB_NAME",
    ):
        MariaDBUsageRepository(replace(config, dashboard_db_host="db.internal"))


def test_repository_allows_a_bounded_cold_view_read() -> None:
    from pipeline.scripts.api.dashboard_usage import DASHBOARD_DB_READ_TIMEOUT_SECONDS

    repository = MariaDBUsageRepository(
        replace(
            config,
            dashboard_db_host="db.internal",
            dashboard_db_user="dashboard_reader",
            dashboard_db_password="test-only",
            dashboard_db_name="llmops",
        )
    )

    assert DASHBOARD_DB_READ_TIMEOUT_SECONDS == 15
    assert repository._connect_args["read_timeout"] == DASHBOARD_DB_READ_TIMEOUT_SECONDS


def test_parallel_repository_matches_serial_result_byte_for_byte() -> None:
    filters = UsageFilters(date(2026, 7, 1), date(2026, 7, 31), "day")
    serial = DeterministicUsageRepository(max_workers=1).fetch(filters)
    parallel = DeterministicUsageRepository(max_workers=4).fetch(filters)

    assert json.dumps(parallel, sort_keys=True, default=str).encode() == json.dumps(
        serial, sort_keys=True, default=str
    ).encode()


def test_parallel_repository_bounds_process_wide_query_concurrency() -> None:
    repository = DeterministicUsageRepository(max_workers=4)
    filters = UsageFilters(date(2026, 7, 1), date(2026, 7, 31), "day")

    with ThreadPoolExecutor(max_workers=2) as callers:
        results = list(callers.map(repository.fetch, (filters, filters)))

    assert 1 < repository.max_active_queries <= 4
    assert len(repository.completed_queries) == 38
    assert results[0] == results[1]


def test_parallel_repository_fails_closed_when_connection_capacity_is_exhausted() -> None:
    def exhausted_connect(**_kwargs: Any) -> Any:
        raise pymysql.OperationalError(1040, "Too many connections")

    repository = MariaDBUsageRepository(
        _dashboard_config(), max_workers=4, connect=exhausted_connect
    )

    with pytest.raises(DashboardQueryError, match="dashboard query failed") as captured:
        repository.fetch(UsageFilters(date(2026, 7, 1), date(2026, 7, 31), "day"))

    assert isinstance(captured.value.__cause__, pymysql.OperationalError)


def test_service_does_not_cache_a_partial_result_when_one_query_fails() -> None:
    repository = DeterministicUsageRepository(max_workers=4, failing_query="chat_user")
    cache = DashboardCache(ttl_seconds=60)
    service = UsageStatsService(repository, cache=cache)
    filters = UsageFilters(date(2026, 7, 1), date(2026, 7, 31), "day")

    with pytest.raises(DashboardQueryError, match="chat_user"):
        service.get(filters)

    assert cache.get(filters.cache_key()) is None


def test_period_sql_survives_pymysql_parameter_interpolation() -> None:
    from pipeline.scripts.api.dashboard_usage import DASHBOARD_SQL, _PERIOD_SQL

    connection = pymysql.Connection(defer_connect=True)
    connection.server_status = 0
    cursor = connection.cursor()
    sql = DASHBOARD_SQL["api_trend"].format(
        period=_PERIOD_SQL["day"].format(column="called_at"),
        api_filter="",
        user_filter="",
    )

    rendered = cursor.mogrify(sql, ("2026-07-01", "2026-08-01"))

    assert "DATE_FORMAT(called_at, '%Y-%m-%d')" in rendered


def test_all_dashboard_queries_bind_for_unfiltered_and_filtered_requests() -> None:
    connection = pymysql.Connection(defer_connect=True)
    connection.server_status = 0
    cursor = connection.cursor()
    repository = MariaDBUsageRepository(_dashboard_config())
    for filters in (
        UsageFilters(date(2026, 7, 1), date(2026, 7, 31), "day"),
        UsageFilters(
            date(2026, 7, 1),
            date(2026, 7, 31),
            "week",
            user_id=34,
            department="JW중외제약",
        ),
    ):
        for query in repository._build_queries(filters):
            assert cursor.mogrify(query.sql, query.params)
