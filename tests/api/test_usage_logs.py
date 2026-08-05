from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pipeline.scripts.api.dashboard_usage import (
    DashboardCache,
    MariaDBUsageRepository,
    UsageLogCursor,
    UsageLogFilters,
    UsageLogPage,
    UsageStatsService,
    decode_usage_log_cursor,
    encode_usage_log_cursor,
    endpoint_feature_label,
)
from pipeline.scripts.api.routes.dashboard_usage import (
    create_usage_dashboard_router,
    create_usage_logs_router,
)


class FakeStatsRepository:
    def fetch(self, _filters):
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


class FakeLogsRepository:
    def __init__(self) -> None:
        self.calls: list[UsageLogFilters] = []

    def fetch_logs(self, filters: UsageLogFilters) -> UsageLogPage:
        self.calls.append(filters)
        return UsageLogPage(
            items=(
                {
                    "user_id": 34,
                    "called_at": datetime(2026, 8, 3, 9, 30),
                    "method": "GET",
                    "path": "/api/brands",
                    "endpoint_label": "브랜드 목록 조회",
                    "http_status": 200,
                    "actor_type": "user",
                    "user_name": "display name",
                    "department": "Market",
                    "request_options": {"query": {"market_id": "ml_001"}},
                },
            ),
            next_cursor=encode_usage_log_cursor(
                UsageLogCursor(datetime(2026, 8, 3, 9, 30), 42)
            ),
            has_more=True,
            page=1,
            total_count=51,
            total_pages=2,
            page_size=50,
        )


def _client(repository: FakeLogsRepository) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_usage_dashboard_router(
            UsageStatsService(FakeStatsRepository(), cache=DashboardCache(ttl_seconds=60))
        )
    )
    app.include_router(create_usage_logs_router(repository))
    return TestClient(app)


def test_usage_logs_requires_a_bounded_date_range() -> None:
    client = _client(FakeLogsRepository())

    missing = client.get("/api/dashboard/usage-logs")
    inverted = client.get(
        "/api/dashboard/usage-logs?date_from=2026-08-03&date_to=2026-08-02"
    )
    oversized = client.get(
        "/api/dashboard/usage-logs?date_from=2026-07-01&date_to=2026-08-03"
    )

    assert missing.status_code == 422
    assert inverted.status_code == 422
    assert oversized.status_code == 422
    assert oversized.json()["detail"] == "조회 기간은 최대 31일입니다."


def test_usage_logs_forwards_exact_filters_and_caps_page_size() -> None:
    repository = FakeLogsRepository()
    client = _client(repository)
    cursor = encode_usage_log_cursor(UsageLogCursor(datetime(2026, 8, 3, 8, 0), 41))

    response = client.get(
        "/api/dashboard/usage-logs",
        params={
            "date_from": "2026-08-01",
            "date_to": "2026-08-03",
            "user_id": 34,
            "user_ids": [34, 35],
            "excluded_user_ids": [82, 85],
            "department": "Market",
            "endpoint": "GET /api/brands",
            "http_status": 200,
            "page_size": 100,
            "cursor": cursor,
        },
    )
    oversized = client.get(
        "/api/dashboard/usage-logs?date_from=2026-08-01&date_to=2026-08-03&page_size=101"
    )

    assert response.status_code == 200
    assert oversized.status_code == 422
    assert repository.calls == [
        UsageLogFilters(
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 3),
            user_id=34,
            user_ids=(34, 35),
            excluded_user_ids=(82, 85),
            department="Market",
            endpoint="GET /api/brands",
            http_status=200,
            page_size=100,
            cursor=UsageLogCursor(datetime(2026, 8, 3, 8, 0), 41),
            page=1,
        )
    ]


def test_usage_logs_rejects_a_tampered_cursor_with_400() -> None:
    response = _client(FakeLogsRepository()).get(
        "/api/dashboard/usage-logs?date_from=2026-08-01&date_to=2026-08-03"
        "&cursor=not-a-valid-cursor"
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "cursor가 올바르지 않습니다."}


def test_cursor_round_trip_is_deterministic() -> None:
    value = UsageLogCursor(datetime(2026, 8, 3, 9, 30, 1, 123456), 123)

    first = encode_usage_log_cursor(value)
    second = encode_usage_log_cursor(value)

    assert first == second
    assert decode_usage_log_cursor(first) == value


def test_usage_logs_response_never_exposes_sensitive_fields() -> None:
    response = _client(FakeLogsRepository()).get(
        "/api/dashboard/usage-logs?date_from=2026-08-01&date_to=2026-08-03"
    )
    payload = response.json()

    assert response.status_code == 200
    assert set(payload) == {
        "items",
        "next_cursor",
        "has_more",
        "page",
        "total_count",
        "total_pages",
        "page_size",
    }
    assert set(payload["items"][0]) == {
        "user_id",
        "called_at",
        "method",
        "path",
        "endpoint_label",
        "http_status",
        "actor_type",
        "user_name",
        "department",
        "request_options",
    }
    assert payload["items"][0]["endpoint_label"] == "브랜드 목록 조회"
    assert payload["items"][0]["request_options"] == {"query": {"market_id": "ml_001"}}
    serialized = response.text.lower()
    assert "actor_uid" not in serialized
    assert "request_params" not in serialized
    assert "jti" not in serialized
    assert "audit_probe" not in serialized
    assert "cursor" not in payload["items"][0]["request_options"].get("query", {})


def test_usage_log_request_options_are_resanitized_and_pruned_in_python() -> None:
    rows = [
        {
            "id": 1,
            "called_at": datetime(2026, 8, 3, 9, 0),
            "endpoint": "POST /api/dynamic-market",
            "http_status": 200,
            "actor_type": "user",
            "user_id": 34,
            "user_name": "display name",
            "department": "Market",
            "request_options": {
                "path": {"brand_name": None, "unknown": "drop"},
                "query": {
                    "market_id": "ml_001",
                    "q": "free text",
                    "audit_probe": "1",
                    "cursor": "opaque",
                    "token": "secret",
                },
                "body": {
                    "view": "sales",
                    "filters": {
                        "analysis_level": "atc4",
                        "unknown": "drop",
                        "auth": "drop",
                    },
                    "options": {"period_range": None, "raw": "drop"},
                    "unknown": "drop",
                },
                "raw": {"query": "drop"},
            },
        }
    ]

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _sql, _params):
            return None

        def fetchall(self):
            return rows

        def fetchone(self):
            return {"total_count": 1}

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            return None

    config = SimpleNamespace(
        dashboard_db_host="db",
        dashboard_db_port=3306,
        dashboard_db_user="reader",
        dashboard_db_password="not-recorded",
        dashboard_db_name="audit",
    )
    repository = MariaDBUsageRepository(config, connect=lambda **_kwargs: Connection())

    page = repository.fetch_logs(UsageLogFilters(date(2026, 8, 1), date(2026, 8, 3)))

    assert page.items[0]["request_options"] == {
        "query": {"market_id": "ml_001"},
        "body": {"view": "sales", "filters": {"analysis_level": "atc4"}},
    }
    options = page.items[0]["request_options"]
    assert not {"q", "audit_probe", "cursor", "token"} & set(options.get("query", {}))
    assert "path" not in options
    assert "options" not in options["body"]
    assert not {"auth", "raw", "unknown"} & set(options["body"]["filters"])


def test_endpoint_feature_labels_cover_stage0_paths_and_unknowns_are_generic() -> None:
    assert endpoint_feature_label("GET", "/api/cause/{brand_name}") == "시장 원인 분석 조회"
    assert endpoint_feature_label("POST", "/api/dynamic-market") == "동적 시장 분석 실행"
    assert endpoint_feature_label("GET", "/api/not-yet-mapped") == "기타 기능"
    assert "/api/not-yet-mapped" not in endpoint_feature_label("GET", "/api/not-yet-mapped")


def test_usage_logs_accept_numbered_pagination_and_reject_cursor_mix() -> None:
    repository = FakeLogsRepository()
    client = _client(repository)
    response = client.get(
        "/api/dashboard/usage-logs?date_from=2026-08-01&date_to=2026-08-03"
        "&page=2&page_size=10"
    )
    assert response.status_code == 200
    assert repository.calls[-1].page == 2
    assert repository.calls[-1].page_size == 10

    mixed = client.get(
        "/api/dashboard/usage-logs?date_from=2026-08-01&date_to=2026-08-03"
        "&page=2&cursor="
        + encode_usage_log_cursor(UsageLogCursor(datetime(2026, 8, 3, 8, 0), 41))
    )
    legacy_size = client.get(
        "/api/dashboard/usage-logs?date_from=2026-08-01&date_to=2026-08-03&page_size=25"
    )

    assert response.json()["page"] == 1
    assert response.json()["total_count"] == 51
    assert mixed.status_code == 422
    assert legacy_size.status_code == 200


def test_repository_query_uses_only_sanitized_views_and_keyset_order() -> None:
    from pipeline.scripts.api.dashboard_usage import USAGE_LOGS_SQL

    normalized = " ".join(USAGE_LOGS_SQL.split()).lower()

    assert "from dashboard_api_usage_v a" in normalized
    assert "left join dashboard_user_directory_v u" in normalized
    assert "audit_api_call_log" not in normalized
    assert "request_params" not in normalized
    assert "jti" not in normalized
    assert "order by a.called_at desc, a.id desc" in normalized
    assert "limit %s" in normalized


def test_repository_fetches_one_extra_row_and_builds_cursor_without_exposing_id() -> None:
    rows = [
        {
            "id": 9,
            "called_at": datetime(2026, 8, 3, 9, 0),
            "endpoint": "GET /api/brands",
            "http_status": 200,
            "actor_type": "user",
            "user_name": "display name",
            "department": "Market",
            "request_options": {"query": {"market_id": "ml_001"}},
        },
        {
            "id": 8,
            "called_at": datetime(2026, 8, 3, 8, 0),
            "endpoint": "POST /api/chat",
            "http_status": 201,
            "actor_type": "service",
            "user_name": None,
            "department": None,
            "request_options": {"body": {"view": "sales"}},
        },
        {
            "id": 7,
            "called_at": datetime(2026, 8, 3, 7, 0),
            "endpoint": "GET /api/market",
            "http_status": 200,
            "actor_type": "unknown",
            "user_name": None,
            "department": None,
            "request_options": {"query": {"q": "should-not-appear"}},
        },
    ]

    class Cursor:
        executed: list[tuple[str, tuple]] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, params):
            self.executed.append((sql, params))

        def fetchall(self):
            return rows

        def fetchone(self):
            return {"total_count": 3}

    class Connection:
        def __init__(self):
            self.db_cursor = Cursor()
            self.closed = False

        def cursor(self):
            return self.db_cursor

        def close(self):
            self.closed = True

    connection = Connection()
    config = SimpleNamespace(
        dashboard_db_host="db",
        dashboard_db_port=3306,
        dashboard_db_user="reader",
        dashboard_db_password="not-recorded",
        dashboard_db_name="audit",
    )
    repository = MariaDBUsageRepository(config, connect=lambda **_kwargs: connection)

    page = repository.fetch_logs(
        UsageLogFilters(date(2026, 8, 1), date(2026, 8, 3), page_size=2)
    )

    assert len(connection.db_cursor.executed) == 2
    assert connection.db_cursor.executed[0][1][-1] == 3
    assert "count(*)" in connection.db_cursor.executed[1][0].lower()
    assert connection.closed is True
    assert page.has_more is True
    assert len(page.items) == 2
    assert all("id" not in item and "endpoint" not in item for item in page.items)
    assert page.items[0]["endpoint_label"] == "브랜드 목록 조회"
    assert page.items[0]["request_options"] == {"query": {"market_id": "ml_001"}}
    assert page.total_count == 3
    assert page.total_pages == 2
    assert decode_usage_log_cursor(page.next_cursor or "") == UsageLogCursor(
        datetime(2026, 8, 3, 8, 0), 8
    )


@pytest.mark.parametrize(
    "endpoint, expected",
    [
        ("GET /api/brands", ("GET", "/api/brands")),
        ("POST /api/v1/market/dynamic/filter/options", ("POST", "/api/v1/market/dynamic/filter/options")),
    ],
)
def test_method_and_path_are_split_without_exposing_the_raw_row(endpoint, expected) -> None:
    from pipeline.scripts.api.dashboard_usage import split_usage_endpoint

    assert split_usage_endpoint(endpoint) == expected
