from __future__ import annotations

import copy
import time
from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from threading import Lock
from typing import Any, Callable, Final, Literal, Protocol

import pymysql

from pipeline.scripts.api.chat_usage_materialization import (
    CHAT_MATERIALIZATION_STATE_SQL,
    CHAT_USAGE_SQL,
    ChatMaterializationState,
    validate_materialization_state,
)
from pipeline.scripts.api.config import APIConfig

Grain = Literal["day", "week"]
MAX_RANGE_DAYS: Final = 366
DEFAULT_CACHE_TTL_SECONDS: Final = 60
DASHBOARD_DB_READ_TIMEOUT_SECONDS: Final = 15
DEFAULT_DASHBOARD_QUERY_WORKERS: Final = 4
MAX_DASHBOARD_QUERY_WORKERS: Final = 8

_PERIOD_SQL: Final = {
    "day": "DATE_FORMAT({column}, '%%Y-%%m-%%d')",
    "week": "DATE_FORMAT(DATE_SUB(DATE({column}), INTERVAL WEEKDAY({column}) DAY), '%%Y-%%m-%%d')",
}

DASHBOARD_SQL: Final[dict[str, str]] = {
    "api_trend": """
        SELECT {period} AS period, COUNT(*) AS total_calls,
               SUM(http_status BETWEEN 200 AND 299) AS successful_calls,
               SUM(actor_type='user') AS attributed_calls
        FROM dashboard_api_usage_v
        WHERE called_at >= %s AND called_at < %s
        {api_filter}
        GROUP BY period ORDER BY period
    """,
    "api_endpoint": """
        SELECT endpoint, COUNT(*) AS calls,
               SUM(http_status BETWEEN 200 AND 299) AS successful_calls
        FROM dashboard_api_usage_v
        WHERE called_at >= %s AND called_at < %s
        {api_filter}
        GROUP BY endpoint ORDER BY calls DESC, endpoint LIMIT 50
    """,
    "api_user": """
        SELECT u.id AS user_id, u.name AS user_name, u.department,
               COUNT(*) AS calls
        FROM dashboard_api_usage_v a
        JOIN dashboard_user_directory_v u
          ON a.actor_uid = CONCAT('genos-user:', u.id)
        WHERE a.called_at >= %s AND a.called_at < %s
        {user_filter}
        GROUP BY u.id, u.name, u.department
        ORDER BY calls DESC, u.id LIMIT 100
    """,
    "api_department": """
        SELECT COALESCE(NULLIF(u.department, ''), '미지정') AS department,
               COUNT(*) AS calls, COUNT(DISTINCT u.id) AS unique_users
        FROM dashboard_api_usage_v a
        JOIN dashboard_user_directory_v u
          ON a.actor_uid = CONCAT('genos-user:', u.id)
        WHERE a.called_at >= %s AND a.called_at < %s
        {user_filter}
        GROUP BY COALESCE(NULLIF(u.department, ''), '미지정')
        ORDER BY calls DESC, department
    """,
    "api_status": """
        SELECT http_status, COUNT(*) AS calls
        FROM dashboard_api_usage_v
        WHERE called_at >= %s AND called_at < %s
        {api_filter}
        GROUP BY http_status ORDER BY calls DESC, http_status
    """,
    "api_weekday_hour": """
        SELECT WEEKDAY(called_at) AS weekday, HOUR(called_at) AS hour,
               COUNT(*) AS calls,
               SUM(http_status NOT BETWEEN 200 AND 299) AS failed_calls
        FROM dashboard_api_usage_v
        WHERE called_at >= %s AND called_at < %s
        {api_filter}
        GROUP BY WEEKDAY(called_at), HOUR(called_at)
        ORDER BY weekday, hour
    """,
    **CHAT_USAGE_SQL,
    "auth_trend": """
        SELECT {period} AS period, type_code, COUNT(*) AS events
        FROM dashboard_auth_event_v a
        LEFT JOIN dashboard_user_directory_v u ON u.id=a.user_id
        WHERE a.reg_date >= %s AND a.reg_date < %s
        {user_filter}
        GROUP BY period, type_code ORDER BY period, type_code
    """,
    "auth_type": """
        SELECT type_code, COUNT(*) AS events
        FROM dashboard_auth_event_v a
        LEFT JOIN dashboard_user_directory_v u ON u.id=a.user_id
        WHERE a.reg_date >= %s AND a.reg_date < %s
        {user_filter}
        GROUP BY type_code ORDER BY events DESC, type_code
    """,
    "auth_hour": """
        SELECT HOUR(a.reg_date) AS hour,
               SUM(a.type_code='AT0001') AS successful_logins,
               SUM(a.type_code='AT0004') AS failed_logins
        FROM dashboard_auth_event_v a
        LEFT JOIN dashboard_user_directory_v u ON u.id=a.user_id
        WHERE a.reg_date >= %s AND a.reg_date < %s
        {user_filter}
        GROUP BY HOUR(a.reg_date) ORDER BY hour
    """,
    "auth_audience": """
        SELECT COALESCE(SUM(first_login >= %s), 0) AS new_users,
               COALESCE(SUM(first_login < %s), 0) AS returning_users
        FROM (
            SELECT history.user_id, MIN(history.reg_date) AS first_login
            FROM dashboard_auth_event_v history
            JOIN (
                SELECT DISTINCT a.user_id
                FROM dashboard_auth_event_v a
                LEFT JOIN dashboard_user_directory_v u ON u.id=a.user_id
                WHERE a.reg_date >= %s AND a.reg_date < %s
                  AND a.type_code='AT0001' AND a.user_id IS NOT NULL
                {user_filter}
            ) active ON active.user_id=history.user_id
            WHERE history.type_code='AT0001'
            GROUP BY history.user_id
        ) audience
    """,
    "credit_trend": """
        SELECT {period} AS period, COUNT(*) AS events,
               SUM(c.user_id IS NOT NULL) AS attributed_events,
               SUM(COALESCE(input_token_count,0)) AS input_tokens,
               SUM(COALESCE(output_token_count,0)) AS output_tokens,
               SUM(COALESCE(charge,0)+COALESCE(overused_charge,0)) AS credits
        FROM dashboard_credit_usage_v c
        LEFT JOIN dashboard_user_directory_v u ON u.id=c.user_id
        WHERE c.reg_date >= %s AND c.reg_date < %s AND c.applied=1
        {user_filter}
        GROUP BY period ORDER BY period
    """,
    "credit_user": """
        SELECT u.id AS user_id, u.name AS user_name, u.department,
               COUNT(*) AS events,
               SUM(COALESCE(c.charge,0)+COALESCE(c.overused_charge,0)) AS credits
        FROM dashboard_credit_usage_v c
        JOIN dashboard_user_directory_v u ON u.id=c.user_id
        WHERE c.reg_date >= %s AND c.reg_date < %s AND c.applied=1
        {user_filter}
        GROUP BY u.id, u.name, u.department
        ORDER BY credits DESC, u.id LIMIT 100
    """,
    "credit_department": """
        SELECT COALESCE(NULLIF(u.department, ''), '미지정') AS department,
               COUNT(*) AS events,
               SUM(COALESCE(c.charge,0)+COALESCE(c.overused_charge,0)) AS credits
        FROM dashboard_credit_usage_v c
        JOIN dashboard_user_directory_v u ON u.id=c.user_id
        WHERE c.reg_date >= %s AND c.reg_date < %s AND c.applied=1
        {user_filter}
        GROUP BY COALESCE(NULLIF(u.department, ''), '미지정')
        ORDER BY credits DESC, department
    """,
    "report_trend": """
        SELECT {period} AS period, COUNT(*) AS attempts,
               SUM(success=1) AS successful_downloads
        FROM dashboard_report_download_v r
        LEFT JOIN dashboard_user_directory_v u
          ON r.actor_uid = CONCAT('genos-user:', u.id)
        WHERE r.completed_at >= %s AND r.completed_at < %s
        {user_filter}
        GROUP BY period ORDER BY period
    """,
    "report_type": """
        SELECT report_type, completion_stage, COUNT(*) AS attempts,
               SUM(success=1) AS successful_downloads
        FROM dashboard_report_download_v r
        LEFT JOIN dashboard_user_directory_v u
          ON r.actor_uid = CONCAT('genos-user:', u.id)
        WHERE r.completed_at >= %s AND r.completed_at < %s
        {user_filter}
        GROUP BY report_type, completion_stage
        ORDER BY attempts DESC, report_type, completion_stage
    """,
    "filter_options": """
        SELECT id AS user_id, name AS user_name, department
        FROM dashboard_user_directory_v
        ORDER BY name, id
    """,
}


@dataclass(frozen=True, slots=True)
class UsageFilters:
    date_from: date
    date_to: date
    grain: Grain
    user_id: int | None = None
    department: str | None = None

    def cache_key(self) -> tuple[Any, ...]:
        return (self.date_from, self.date_to, self.grain, self.user_id, self.department)


class UsageRepository(Protocol):
    def fetch(self, filters: UsageFilters) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class DashboardQuery:
    name: str
    ordinal: int
    sql: str
    params: tuple[Any, ...]


class DashboardQueryError(RuntimeError):
    def __init__(self, query_name: str) -> None:
        super().__init__(f"dashboard query failed: {query_name}")
        self.query_name = query_name


class DashboardCache:
    def __init__(self, *, ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._values: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
        self._lock = Lock()

    def get(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        with self._lock:
            entry = self._values.get(key)
            if entry is None or time.monotonic() - entry[0] >= self.ttl_seconds:
                self._values.pop(key, None)
                return None
            return copy.deepcopy(entry[1])

    def put(self, key: tuple[Any, ...], value: dict[str, Any]) -> None:
        with self._lock:
            self._values[key] = (time.monotonic(), copy.deepcopy(value))


class MariaDBUsageRepository:
    def __init__(
        self,
        config: APIConfig,
        *,
        max_workers: int = DEFAULT_DASHBOARD_QUERY_WORKERS,
        connect: Callable[..., Any] = pymysql.connect,
    ) -> None:
        required = {
            "DASHBOARD_DB_HOST": config.dashboard_db_host,
            "DASHBOARD_DB_USER": config.dashboard_db_user,
            "DASHBOARD_DB_PASSWORD": config.dashboard_db_password,
            "DASHBOARD_DB_NAME": config.dashboard_db_name,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError("dashboard reader settings are missing: " + ", ".join(missing))
        if not 1 <= max_workers <= MAX_DASHBOARD_QUERY_WORKERS:
            raise ValueError(
                f"dashboard query workers must be between 1 and {MAX_DASHBOARD_QUERY_WORKERS}"
            )
        self._connect_args = {
            "host": config.dashboard_db_host,
            "port": config.dashboard_db_port,
            "user": config.dashboard_db_user,
            "password": config.dashboard_db_password,
            "database": config.dashboard_db_name,
            "charset": "utf8mb4",
            "cursorclass": pymysql.cursors.DictCursor,
            "autocommit": True,
            "read_timeout": DASHBOARD_DB_READ_TIMEOUT_SECONDS,
            "write_timeout": 8,
            "connect_timeout": 3,
        }
        self._connect = connect
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="dashboard-query",
        )

    def fetch(self, filters: UsageFilters) -> dict[str, Any]:
        try:
            state = self._fetch_chat_materialization_state()
        except Exception as exc:
            raise DashboardQueryError("chat_materialization_state") from exc
        validate_materialization_state(state, filters)
        rows = self._fetch_rows(self._build_queries(filters))

        options = rows["filter_options"]
        return {
            "api": {
                "trend": rows["api_trend"],
                "by_endpoint": rows["api_endpoint"],
                "by_user": rows["api_user"],
                "by_department": rows["api_department"],
                "by_status": rows["api_status"],
                "by_weekday_hour": rows["api_weekday_hour"],
            },
            "chat": {
                "trend": rows["chat_trend"],
                "by_user": rows["chat_user"],
                "by_user_service": rows["chat_user_service"],
            },
            "auth": {
                "trend": rows["auth_trend"],
                "by_type": rows["auth_type"],
                "by_hour": rows["auth_hour"],
                "audience": (rows["auth_audience"] or [{"new_users": 0, "returning_users": 0}])[0],
            },
            "credit": {
                "trend": rows["credit_trend"],
                "by_user": rows["credit_user"],
                "by_department": rows["credit_department"],
            },
            "reports": {"trend": rows["report_trend"], "by_type": rows["report_type"]},
            "filter_options": {
                "users": options,
                "departments": sorted(
                    {row["department"] for row in options if row.get("department")}
                ),
            },
        }

    def _build_queries(self, filters: UsageFilters) -> tuple[DashboardQuery, ...]:
        start = filters.date_from.isoformat()
        end_exclusive = (filters.date_to + timedelta(days=1)).isoformat()
        period = _PERIOD_SQL[filters.grain]
        user_filter, user_params = _user_filter(filters)
        api_filter, api_params = _api_filter(filters)
        queries: list[DashboardQuery] = []
        for ordinal, (name, template) in enumerate(DASHBOARD_SQL.items()):
            sql = template.format(
                period=period.format(column=_time_column(name)),
                user_filter=user_filter,
                api_filter=api_filter,
            )
            if name == "filter_options":
                params: tuple[Any, ...] = ()
            elif name == "auth_audience":
                params = (start, start, start, end_exclusive, *user_params)
            elif name == "chat_trend":
                params = (
                    start,
                    end_exclusive,
                    *user_params,
                    start,
                    end_exclusive,
                    *user_params,
                )
            elif name in {"chat_user", "chat_user_service"}:
                params = (start, end_exclusive, start, end_exclusive, *user_params)
            elif name.startswith("api_") and name not in {"api_user", "api_department"}:
                params = (start, end_exclusive, *api_params)
            else:
                params = (start, end_exclusive, *user_params)
            queries.append(DashboardQuery(name=name, ordinal=ordinal, sql=sql, params=params))
        return tuple(queries)

    def _fetch_chat_materialization_state(self) -> ChatMaterializationState | None:
        connection = self._connect(**self._connect_args)
        try:
            with connection.cursor() as cursor:
                cursor.execute(CHAT_MATERIALIZATION_STATE_SQL)
                row = cursor.fetchone()
        finally:
            connection.close()
        return ChatMaterializationState.from_row(row) if row else None

    def _fetch_rows(self, queries: tuple[DashboardQuery, ...]) -> dict[str, list[dict[str, Any]]]:
        futures: dict[Future[tuple[str, list[dict[str, Any]]]], DashboardQuery] = {
            self._executor.submit(self._execute_query, query): query for query in queries
        }
        done, pending = wait(futures, return_when=FIRST_EXCEPTION)
        failed = [
            (query, future.exception())
            for future, query in futures.items()
            if future in done and future.exception() is not None
        ]
        if failed:
            for future in pending:
                future.cancel()
            query, error = min(failed, key=lambda item: item[0].ordinal)
            raise DashboardQueryError(query.name) from error

        results = {future.result()[0]: future.result()[1] for future in futures}
        return {query.name: results[query.name] for query in queries}

    def _execute_query(self, query: DashboardQuery) -> tuple[str, list[dict[str, Any]]]:
        connection = self._connect(**self._connect_args)
        try:
            with connection.cursor() as cursor:
                cursor.execute(query.sql, query.params)
                rows = [_normalize_row(row) for row in cursor.fetchall()]
        finally:
            connection.close()
        return query.name, rows


class UsageStatsService:
    AUTH_LABELS: Final = {
        "AT0001": "로그인 성공",
        "AT0002": "로그아웃",
        "AT0003": "세션 갱신",
        "AT0004": "로그인 실패",
        "AT0005": "비밀번호 변경",
        "AT0006": "사용자 등록",
    }

    def __init__(self, repository: UsageRepository, *, cache: DashboardCache) -> None:
        self._repository = repository
        self._cache = cache

    @staticmethod
    def service_category(service_id: int | None) -> str:
        if service_id == 61:
            return "rnd"
        if service_id in {91, 94}:
            return "market"
        return "unknown"

    def get(self, filters: UsageFilters) -> dict[str, Any]:
        cached = self._cache.get(filters.cache_key())
        if cached is not None:
            return cached
        result = self._repository.fetch(filters)
        for row in result["chat"]["trend"]:
            row["service_category"] = self.service_category(row.get("service_id"))
        for row in result["chat"]["by_user_service"]:
            row["service_category"] = self.service_category(row.get("service_id"))
        for row in result["auth"]["trend"] + result["auth"]["by_type"]:
            row["label"] = self.AUTH_LABELS.get(str(row.get("type_code")), "기타 인증 이벤트")
        payload = {
            "filters": asdict(filters),
            "limits": {"max_days": MAX_RANGE_DAYS, "cache_ttl_seconds": self._cache.ttl_seconds},
            "data_quality": _data_quality(result),
            **result,
        }
        self._cache.put(filters.cache_key(), payload)
        return payload


def _user_filter(filters: UsageFilters) -> tuple[str, tuple[Any, ...]]:
    clauses: list[str] = []
    params: list[Any] = []
    if filters.user_id is not None:
        clauses.append("u.id=%s")
        params.append(filters.user_id)
    if filters.department:
        clauses.append("u.department=%s")
        params.append(filters.department)
    return ((" AND " + " AND ".join(clauses)) if clauses else "", tuple(params))


def _api_filter(filters: UsageFilters) -> tuple[str, tuple[Any, ...]]:
    clauses: list[str] = []
    params: list[Any] = []
    if filters.user_id is not None:
        clauses.append("actor_uid=%s")
        params.append(f"genos-user:{filters.user_id}")
    if filters.department:
        clauses.append(
            "actor_uid IN (SELECT CONCAT('genos-user:',id) FROM dashboard_user_directory_v WHERE department=%s)"
        )
        params.append(filters.department)
    return ((" AND " + " AND ".join(clauses)) if clauses else "", tuple(params))


def _time_column(query_name: str) -> str:
    if query_name.startswith("api_"):
        return "called_at"
    if query_name.startswith("chat_"):
        return "usage_date"
    if query_name.startswith(("auth_", "credit_")):
        return "reg_date"
    return "completed_at"


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: float(value) if isinstance(value, Decimal) else value
        for key, value in row.items()
    }


def _data_quality(result: dict[str, Any]) -> dict[str, int]:
    api_total = sum(int(row.get("total_calls") or 0) for row in result["api"]["trend"])
    api_attributed = sum(int(row.get("attributed_calls") or 0) for row in result["api"]["trend"])
    chat_total = sum(int(row.get("turns") or 0) for row in result["chat"]["trend"])
    chat_attributed = sum(int(row.get("attributed_turns") or 0) for row in result["chat"]["trend"])
    credit_total = sum(int(row.get("events") or 0) for row in result["credit"]["trend"])
    credit_attributed = sum(
        int(row.get("attributed_events") or 0) for row in result["credit"]["trend"]
    )
    return {
        "api_attributed_calls": api_attributed,
        "api_unknown_calls": max(api_total - api_attributed, 0),
        "chat_attributed_turns": chat_attributed,
        "chat_unknown_turns": max(chat_total - chat_attributed, 0),
        "credit_attributed_events": credit_attributed,
        "credit_unknown_events": max(credit_total - credit_attributed, 0),
    }
