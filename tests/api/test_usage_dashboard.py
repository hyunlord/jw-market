from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
import pymysql
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pipeline.scripts.api.config import config
from pipeline.scripts.api.dashboard_usage import (
    DashboardCache,
    MariaDBUsageRepository,
    UsageFilters,
    UsageStatsService,
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
    assert "dashboard_chat_usage_v" in sql
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
    from pipeline.scripts.api.dashboard_usage import (
        DASHBOARD_SQL,
        _PERIOD_SQL,
        _api_filter,
        _time_column,
        _user_filter,
    )

    connection = pymysql.Connection(defer_connect=True)
    connection.server_status = 0
    cursor = connection.cursor()
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
        user_filter, user_params = _user_filter(filters)
        api_filter, api_params = _api_filter(filters)
        for name, template in DASHBOARD_SQL.items():
            sql = template.format(
                period=_PERIOD_SQL[filters.grain].format(column=_time_column(name)),
                user_filter=user_filter,
                api_filter=api_filter,
            )
            if name == "filter_options":
                params = ()
            elif name == "auth_audience":
                params = (
                    filters.date_from.isoformat(),
                    filters.date_from.isoformat(),
                    filters.date_from.isoformat(),
                    "2026-08-01",
                    *user_params,
                )
            elif name.startswith("api_") and name not in {"api_user", "api_department"}:
                params = (filters.date_from.isoformat(), "2026-08-01", *api_params)
            else:
                params = (filters.date_from.isoformat(), "2026-08-01", *user_params)

            assert cursor.mogrify(sql, params)
