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
            "api": {"trend": [], "by_endpoint": [], "by_user": [], "by_department": []},
            "chat": {"trend": [], "by_user": []},
            "auth": {"trend": [], "by_type": []},
            "credit": {"trend": [], "by_user": []},
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
