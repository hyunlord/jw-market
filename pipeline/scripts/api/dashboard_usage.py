from __future__ import annotations

import base64
import binascii
import copy
import json
import time
from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
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
MAX_USAGE_LOG_RANGE_DAYS: Final = 31
DEFAULT_USAGE_LOG_PAGE_SIZE: Final = 50
MAX_USAGE_LOG_PAGE_SIZE: Final = 100
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
        ORDER BY calls DESC, u.id LIMIT 200
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
    "auth_user": """
        SELECT u.id AS user_id, u.name AS user_name, u.department,
               COUNT(*) AS events
        FROM dashboard_auth_event_v a
        JOIN dashboard_user_directory_v u ON u.id=a.user_id
        WHERE a.reg_date >= %s AND a.reg_date < %s
        {user_filter}
        GROUP BY u.id, u.name, u.department
        ORDER BY events DESC, u.id LIMIT 200
    """,
    "auth_department": """
        SELECT COALESCE(NULLIF(u.department, ''), '미지정') AS department,
               COUNT(*) AS events, COUNT(DISTINCT u.id) AS unique_users
        FROM dashboard_auth_event_v a
        JOIN dashboard_user_directory_v u ON u.id=a.user_id
        WHERE a.reg_date >= %s AND a.reg_date < %s
        {user_filter}
        GROUP BY COALESCE(NULLIF(u.department, ''), '미지정')
        ORDER BY events DESC, department
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
        ORDER BY credits DESC, u.id LIMIT 200
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
    "report_user": """
        SELECT u.id AS user_id, u.name AS user_name, u.department,
               COUNT(*) AS attempts, SUM(r.success=1) AS successful_downloads
        FROM dashboard_report_download_v r
        JOIN dashboard_user_directory_v u
          ON r.actor_uid = CONCAT('genos-user:', u.id)
        WHERE r.completed_at >= %s AND r.completed_at < %s
        {user_filter}
        GROUP BY u.id, u.name, u.department
        ORDER BY attempts DESC, u.id LIMIT 200
    """,
    "report_department": """
        SELECT COALESCE(NULLIF(u.department, ''), '미지정') AS department,
               COUNT(*) AS attempts, SUM(r.success=1) AS successful_downloads,
               COUNT(DISTINCT u.id) AS unique_users
        FROM dashboard_report_download_v r
        JOIN dashboard_user_directory_v u
          ON r.actor_uid = CONCAT('genos-user:', u.id)
        WHERE r.completed_at >= %s AND r.completed_at < %s
        {user_filter}
        GROUP BY COALESCE(NULLIF(u.department, ''), '미지정')
        ORDER BY attempts DESC, department
    """,
    "filter_options": """
        SELECT id AS user_id, name AS user_name, department
        FROM dashboard_user_directory_v u
        WHERE 1=1 {user_filter}
        ORDER BY name, id
    """,
}

COMPARISON_SQL: Final[dict[str, str]] = {
    "api": """
        SELECT COUNT(*) AS value
        FROM dashboard_api_usage_v
        WHERE called_at >= %s AND called_at < %s
        {api_filter}
    """,
    "chat_sessions": """
        SELECT COUNT(DISTINCT s.conversation_id) AS value
        FROM jw_mart.mart_chat_usage_daily_session s
        LEFT JOIN dashboard_user_directory_v u ON u.id=s.portal_user_id
        WHERE s.usage_date >= %s AND s.usage_date < %s
          AND s.service_id IN (61, 91, 94)
        {user_filter}
    """,
    "successful_logins": """
        SELECT COUNT(*) AS value
        FROM dashboard_auth_event_v a
        LEFT JOIN dashboard_user_directory_v u ON u.id=a.user_id
        WHERE a.reg_date >= %s AND a.reg_date < %s AND a.type_code='AT0001'
        {user_filter}
    """,
    "credits": """
        SELECT COALESCE(SUM(COALESCE(c.charge,0)+COALESCE(c.overused_charge,0)),0) AS value
        FROM dashboard_credit_usage_v c
        LEFT JOIN dashboard_user_directory_v u ON u.id=c.user_id
        WHERE c.reg_date >= %s AND c.reg_date < %s AND c.applied=1
        {user_filter}
    """,
    "successful_downloads": """
        SELECT COALESCE(SUM(r.success=1),0) AS value
        FROM dashboard_report_download_v r
        LEFT JOIN dashboard_user_directory_v u
          ON r.actor_uid=CONCAT('genos-user:', u.id)
        WHERE r.completed_at >= %s AND r.completed_at < %s
        {user_filter}
    """,
}

USAGE_LOGS_SQL: Final = """
    SELECT a.id, a.called_at, a.endpoint, a.request_options, a.http_status, a.actor_type,
           CASE WHEN a.actor_type='user' THEN u.id ELSE NULL END AS user_id,
           CASE WHEN a.actor_type='user' THEN u.name ELSE NULL END AS user_name,
           CASE WHEN a.actor_type='user' THEN u.department ELSE NULL END AS department
    FROM dashboard_api_usage_v a
    LEFT JOIN dashboard_user_directory_v u
      ON a.actor_type='user' AND a.actor_uid=CONCAT('genos-user:', u.id)
    WHERE a.called_at >= %s AND a.called_at < %s
    {filters}
    ORDER BY a.called_at DESC, a.id DESC
    LIMIT %s
    {offset}
"""

USAGE_LOGS_COUNT_SQL: Final = """
    SELECT COUNT(*) AS total_count
    FROM dashboard_api_usage_v a
    LEFT JOIN dashboard_user_directory_v u
      ON a.actor_type='user' AND a.actor_uid=CONCAT('genos-user:', u.id)
    WHERE a.called_at >= %s AND a.called_at < %s
    {filters}
"""

_CHAT_TURN_SOURCE_SQL: Final = (
    "CASE WHEN c.conversation_log_id IS NULL THEN 'rnd' ELSE 'market' END"
)
_CHAT_TURN_ID_SQL: Final = """
    CASE WHEN c.conversation_log_id IS NULL THEN
        SHA2(CONCAT('rnd:', CHAR_LENGTH(c.conversation_id), ':', c.conversation_id,
                    ':', LPAD(c.turn_index, 10, '0'),
                    ':', CHAR_LENGTH(c.trace_id), ':', c.trace_id), 256)
    ELSE SHA2(CONCAT('market:', c.conversation_log_id), 256) END
""".strip()

CHAT_TURNS_SQL: Final = f"""
    SELECT c.conversation_log_id, c.created_at, c.service_id,
           c.portal_user_id AS user_id, u.name AS user_name, u.department,
           c.conversation_id, c.turn_index, c.contract_status, c.quality_label,
           c.elapsed_ms, c.input_tokens, c.output_tokens, c.total_tokens,
           c.trace_id, d.question_text, {_CHAT_TURN_SOURCE_SQL} AS source,
           COALESCE(c.trace_id, CONCAT('conversation-log:', c.conversation_log_id)) AS detail_key,
           {_CHAT_TURN_ID_SQL} AS source_turn_id
    FROM dashboard_chat_usage_v c
    JOIN dashboard_user_directory_v u ON u.id=c.portal_user_id
    LEFT JOIN dashboard_chat_turn_detail_v d
      ON d.detail_key=COALESCE(c.trace_id, CONCAT('conversation-log:', c.conversation_log_id))
    WHERE c.created_at >= %s AND c.created_at < %s
      AND c.portal_user_id IS NOT NULL
      AND c.service_id IN (61, 91, 94)
    {{filters}}
    ORDER BY c.created_at DESC, source DESC, source_turn_id DESC
    LIMIT %s
    {{offset}}
"""

CHAT_TURN_DETAIL_SQL: Final = """
    SELECT detail_key, question_text, answer_text
    FROM dashboard_chat_turn_detail_v
    WHERE detail_key = %s
"""

CHAT_RND_SESSION_DETAIL_SQL: Final = """
    SELECT c.trace_id AS detail_key, c.turn_index, c.created_at,
           d.question_text, d.answer_text
    FROM dashboard_chat_usage_v c
    LEFT JOIN dashboard_chat_turn_detail_v d ON d.detail_key=c.trace_id
    WHERE c.conversation_log_id IS NULL
      AND c.service_id=61
      AND c.conversation_id=%s
    ORDER BY c.turn_index ASC, c.created_at ASC, c.trace_id ASC
"""

CHAT_TURNS_COUNT_SQL: Final = f"""
    SELECT COUNT(*) AS total_count
    FROM dashboard_chat_usage_v c
    JOIN dashboard_user_directory_v u ON u.id=c.portal_user_id
    WHERE c.created_at >= %s AND c.created_at < %s
      AND c.portal_user_id IS NOT NULL
      AND c.service_id IN (61, 91, 94)
    {{filters}}
"""

ENDPOINT_LABELS_PATH: Final = Path(__file__).with_name("usage_endpoint_labels.json")
UNKNOWN_ENDPOINT_LABEL: Final = "기타 기능"
_ENDPOINT_LABELS: Final[dict[str, str]] = json.loads(
    ENDPOINT_LABELS_PATH.read_text(encoding="utf-8")
)
_REQUEST_OPTIONS_ALLOWED: Final[dict[str, tuple[str, ...]]] = {
    "path": ("brand_name",),
    "query": (
        "market_id",
        "view",
        "source",
        "measure",
        "brand",
        "brand_name",
        "atc4_codes",
    ),
    "body": (
        "view",
        "source",
        "measure",
        "selected_brand",
        "period",
        "period_start",
        "period_end",
        "csd_channel",
        "mode",
        "specialty",
        "visit_location",
        "top_n",
    ),
    "body.filters": (
        "analysis_level",
        "atc",
        "atc4",
        "channel",
        "focus_brand_key",
        "view_kind",
    ),
    "body.options": ("period_range",),
}


@dataclass(frozen=True, slots=True)
class UsageFilters:
    date_from: date
    date_to: date
    grain: Grain
    user_id: int | None = None
    user_ids: tuple[int, ...] = ()
    department: str | None = None
    excluded_user_ids: tuple[int, ...] = ()

    def cache_key(self) -> tuple[Any, ...]:
        return (
            self.date_from,
            self.date_to,
            self.grain,
            self.user_id,
            self.user_ids,
            self.department,
            self.excluded_user_ids,
        )


@dataclass(frozen=True, slots=True)
class UsageLogCursor:
    called_at: datetime
    id: int


@dataclass(frozen=True, slots=True)
class UsageLogFilters:
    date_from: date
    date_to: date
    user_id: int | None = None
    user_ids: tuple[int, ...] = ()
    excluded_user_ids: tuple[int, ...] = ()
    department: str | None = None
    endpoint: str | None = None
    http_status: int | None = None
    page_size: int = DEFAULT_USAGE_LOG_PAGE_SIZE
    cursor: UsageLogCursor | None = None
    page: int = 1


@dataclass(frozen=True, slots=True)
class UsageLogPage:
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None
    has_more: bool
    page: int
    total_count: int
    total_pages: int
    page_size: int


class InvalidUsageLogCursor(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ChatTurnCursor:
    created_at: datetime
    source: Literal["market", "rnd"]
    source_turn_id: str


@dataclass(frozen=True, slots=True)
class ChatTurnFilters:
    date_from: date
    date_to: date
    user_id: int | None = None
    user_ids: tuple[int, ...] = ()
    excluded_user_ids: tuple[int, ...] = ()
    department: str | None = None
    page_size: int = DEFAULT_USAGE_LOG_PAGE_SIZE
    cursor: ChatTurnCursor | None = None
    page: int = 1


@dataclass(frozen=True, slots=True)
class ChatTurnPage:
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None
    has_more: bool
    page: int
    total_count: int
    total_pages: int
    page_size: int


@dataclass(frozen=True, slots=True)
class ChatTurnDetail:
    item: dict[str, Any]


class InvalidChatTurnCursor(ValueError):
    pass


class ChatTurnDataContractError(RuntimeError):
    pass


def encode_chat_turn_id(detail_key: str) -> str:
    if not isinstance(detail_key, str) or not detail_key or len(detail_key) > 128:
        raise ValueError("invalid chat turn id")
    payload = json.dumps(
        {"v": 2, "detail_key": detail_key},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "t2_" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_chat_turn_id(value: str) -> str:
    if not value.startswith("t2_"):
        raise ValueError("invalid chat turn id")
    try:
        padding = "=" * (-len(value[3:]) % 4)
        payload = json.loads(
            base64.b64decode(value[3:] + padding, altchars=b"-_", validate=True)
        )
        detail_key = payload["detail_key"]
        if (
            payload.get("v") != 2
            or not isinstance(detail_key, str)
            or not detail_key
            or len(detail_key) > 128
        ):
            raise ValueError("invalid chat turn id")
        return detail_key
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid chat turn id") from exc


def encode_usage_log_cursor(cursor: UsageLogCursor) -> str:
    payload = json.dumps(
        {"called_at": cursor.called_at.isoformat(timespec="microseconds"), "id": cursor.id},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_usage_log_cursor(value: str) -> UsageLogCursor:
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("ascii"))
        if not isinstance(payload, dict) or set(payload) != {"called_at", "id"}:
            raise InvalidUsageLogCursor
        called_at_raw = payload["called_at"]
        row_id = payload["id"]
        if not isinstance(called_at_raw, str) or not isinstance(row_id, int) or row_id < 1:
            raise InvalidUsageLogCursor
        called_at = datetime.fromisoformat(called_at_raw)
        if called_at.tzinfo is not None:
            raise InvalidUsageLogCursor
        cursor = UsageLogCursor(called_at=called_at, id=row_id)
        if encode_usage_log_cursor(cursor) != value:
            raise InvalidUsageLogCursor
        return cursor
    except InvalidUsageLogCursor:
        raise
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise InvalidUsageLogCursor from exc


def encode_chat_turn_cursor(cursor: ChatTurnCursor) -> str:
    payload = json.dumps(
        {
            "v": 2,
            "created_at": cursor.created_at.isoformat(timespec="microseconds"),
            "source": cursor.source,
            "source_turn_id": cursor.source_turn_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_chat_turn_cursor(value: str) -> ChatTurnCursor:
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("ascii"))
        if not isinstance(payload, dict) or set(payload) != {
            "v",
            "created_at",
            "source",
            "source_turn_id",
        }:
            raise InvalidChatTurnCursor
        version = payload["v"]
        created_at_raw = payload["created_at"]
        source = payload["source"]
        source_turn_id = payload["source_turn_id"]
        if (
            version != 2
            or not isinstance(created_at_raw, str)
            or source not in {"market", "rnd"}
            or not isinstance(source_turn_id, str)
            or len(source_turn_id) != 64
            or any(character not in "0123456789abcdef" for character in source_turn_id)
        ):
            raise InvalidChatTurnCursor
        created_at = datetime.fromisoformat(created_at_raw)
        if created_at.tzinfo is not None:
            raise InvalidChatTurnCursor
        cursor = ChatTurnCursor(
            created_at=created_at,
            source=source,
            source_turn_id=source_turn_id,
        )
        if encode_chat_turn_cursor(cursor) != value:
            raise InvalidChatTurnCursor
        return cursor
    except InvalidChatTurnCursor:
        raise
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise InvalidChatTurnCursor from exc


class UsageRepository(Protocol):
    def fetch(self, filters: UsageFilters) -> dict[str, Any]: ...


class UsageLogsRepository(Protocol):
    def fetch_logs(self, filters: UsageLogFilters) -> UsageLogPage: ...


class ChatTurnsRepository(Protocol):
    def fetch_chat_turns(self, filters: ChatTurnFilters) -> ChatTurnPage: ...

    def fetch_chat_turn(self, detail_key: str) -> ChatTurnDetail | None: ...


class UsageHistoryRepository(UsageLogsRepository, ChatTurnsRepository, Protocol):
    pass


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
        previous_window = comparison_window(filters, state)
        queries = self._build_queries(filters)
        comparison_queries = (
            self._build_comparison_queries(
                filters,
                previous_window,
                ordinal_offset=len(queries),
            )
            if previous_window is not None
            else ()
        )
        rows, failed_optional = self._fetch_rows_with_optional(queries, comparison_queries)
        previous_rows = {
            name: value for name, value in rows.items() if name.startswith("comparison_")
        }
        comparison_failure_reason: str | None = None
        if failed_optional:
            comparison_failure_reason = "comparison_query_unavailable"

        options = rows["filter_options"]
        result = {
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
                "by_user": rows["auth_user"],
                "by_department": rows["auth_department"],
            },
            "credit": {
                "trend": rows["credit_trend"],
                "by_user": rows["credit_user"],
                "by_department": rows["credit_department"],
            },
            "reports": {
                "trend": rows["report_trend"],
                "by_type": rows["report_type"],
                "by_user": rows["report_user"],
                "by_department": rows["report_department"],
            },
            "filter_options": {
                "users": options,
                "departments": sorted(
                    {row["department"] for row in options if row.get("department")}
                ),
            },
        }
        result["comparison"] = build_usage_comparison(
            result,
            filters=filters,
            previous_window=previous_window,
            previous_rows=previous_rows,
            unavailable_reason=comparison_failure_reason,
        )
        return result

    def fetch_logs(self, filters: UsageLogFilters) -> UsageLogPage:
        clauses, base_params = _usage_log_query_parts(filters)
        params = list(base_params)
        params.append(filters.page_size + 1)
        offset_sql = ""
        if filters.cursor is None and filters.page > 1:
            offset_sql = "OFFSET %s"
            params.append((filters.page - 1) * filters.page_size)
        sql = USAGE_LOGS_SQL.format(
            filters=_and_clauses(clauses),
            offset=offset_sql,
        )

        connection = self._connect(**self._connect_args)
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                rows = [_normalize_row(row) for row in cursor.fetchall()]
                cursor.execute(
                    USAGE_LOGS_COUNT_SQL.format(filters=_and_clauses(clauses)),
                    base_params,
                )
                total_count = _total_count(cursor.fetchone())
        finally:
            connection.close()

        has_more = len(rows) > filters.page_size
        page_rows = rows[: filters.page_size]
        items: list[dict[str, Any]] = []
        for row in page_rows:
            method, path = split_usage_endpoint(str(row["endpoint"]))
            items.append(
                {
                    "user_id": row.get("user_id"),
                    "called_at": row["called_at"],
                    "method": method,
                    "path": path,
                    "endpoint_label": endpoint_feature_label(method, path),
                    "http_status": row["http_status"],
                    "actor_type": row["actor_type"],
                    "user_name": row.get("user_name"),
                    "department": row.get("department"),
                    "request_options": _request_options(row.get("request_options")),
                }
            )
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = encode_usage_log_cursor(
                UsageLogCursor(called_at=last["called_at"], id=int(last["id"]))
            )
        return UsageLogPage(
            items=tuple(items),
            next_cursor=next_cursor,
            has_more=has_more,
            page=filters.page,
            total_count=total_count,
            total_pages=_total_pages(total_count, filters.page_size),
            page_size=filters.page_size,
        )

    def fetch_chat_turns(self, filters: ChatTurnFilters) -> ChatTurnPage:
        try:
            state = self._fetch_chat_materialization_state()
        except Exception as exc:
            raise DashboardQueryError("chat_materialization_state") from exc
        validate_materialization_state(state, filters)

        clauses, base_params = _chat_turn_query_parts(filters)
        params = list(base_params)
        params.append(filters.page_size + 1)
        offset_sql = ""
        if filters.cursor is None and filters.page > 1:
            offset_sql = "OFFSET %s"
            params.append((filters.page - 1) * filters.page_size)
        sql = CHAT_TURNS_SQL.format(
            filters=_and_clauses(clauses),
            offset=offset_sql,
        )

        connection = self._connect(**self._connect_args)
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                rows = [_normalize_row(row) for row in cursor.fetchall()]
                cursor.execute(
                    CHAT_TURNS_COUNT_SQL.format(filters=_and_clauses(clauses)),
                    base_params,
                )
                total_count = _total_count(cursor.fetchone())
        except Exception as exc:
            raise DashboardQueryError("chat_turns") from exc
        finally:
            connection.close()

        has_more = len(rows) > filters.page_size
        page_rows = rows[: filters.page_size]
        try:
            items = tuple(_chat_turn_item(row) for row in page_rows)
            next_cursor = None
            if has_more and page_rows:
                last = page_rows[-1]
                next_cursor = encode_chat_turn_cursor(
                    ChatTurnCursor(
                        created_at=last["created_at"],
                        source=last["source"],
                        source_turn_id=last["source_turn_id"],
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise ChatTurnDataContractError("invalid chat turn cursor identity") from exc
        return ChatTurnPage(
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
            page=filters.page,
            total_count=total_count,
            total_pages=_total_pages(total_count, filters.page_size),
            page_size=filters.page_size,
        )

    def fetch_chat_turn(self, detail_key: str) -> ChatTurnDetail | None:
        connection = self._connect(**self._connect_args)
        try:
            with connection.cursor() as cursor:
                if detail_key.startswith("rnd-session:"):
                    cursor.execute(CHAT_RND_SESSION_DETAIL_SQL, (detail_key[12:],))
                    rows = cursor.fetchall()
                    row = rows[0] if rows else None
                else:
                    cursor.execute(CHAT_TURN_DETAIL_SQL, (detail_key,))
                    row = cursor.fetchone()
                    rows = [row] if row else []
        except Exception as exc:
            raise DashboardQueryError("chat_turn_detail") from exc
        finally:
            connection.close()
        if row is None:
            return None
        normalized_rows = [_normalize_row(item) for item in rows]
        normalized = normalized_rows[0]
        return ChatTurnDetail(
            item={
                "turn_id": encode_chat_turn_id(detail_key),
                "question_text": normalized.get("question_text"),
                "answer_text": normalized_rows[-1].get("answer_text"),
                "source": "rnd" if detail_key.startswith("rnd-session:") else "market",
                "recorded_stage_count": len(normalized_rows),
                "stages": [
                    {
                        "turn_index": item.get("turn_index"),
                        "created_at": item.get("created_at"),
                        "question_text": item.get("question_text"),
                        "answer_text": item.get("answer_text"),
                    }
                    for item in normalized_rows
                ],
            }
        )

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
                params = user_params
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

    def _build_comparison_queries(
        self,
        filters: UsageFilters,
        window: tuple[date, date],
        *,
        ordinal_offset: int = 0,
    ) -> tuple[DashboardQuery, ...]:
        start = window[0].isoformat()
        end_exclusive = (window[1] + timedelta(days=1)).isoformat()
        user_filter, user_params = _user_filter(filters)
        api_filter, api_params = _api_filter(filters)
        queries: list[DashboardQuery] = []
        for ordinal, (name, template) in enumerate(COMPARISON_SQL.items()):
            sql = template.format(user_filter=user_filter, api_filter=api_filter)
            params = (
                (start, end_exclusive, *api_params)
                if name == "api"
                else (start, end_exclusive, *user_params)
            )
            queries.append(
                DashboardQuery(
                    name=f"comparison_{name}",
                    ordinal=ordinal_offset + ordinal,
                    sql=sql,
                    params=params,
                )
            )
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

    def _fetch_rows_with_optional(
        self,
        required: tuple[DashboardQuery, ...],
        optional: tuple[DashboardQuery, ...],
    ) -> tuple[dict[str, list[dict[str, Any]]], tuple[str, ...]]:
        optional_names = {query.name for query in optional}
        all_queries = required + optional
        futures: dict[Future[tuple[str, list[dict[str, Any]]]], DashboardQuery] = {
            self._executor.submit(self._execute_query, query): query for query in all_queries
        }
        pending = set(futures)
        results: dict[str, list[dict[str, Any]]] = {}
        failed_optional: list[str] = []
        while pending:
            done, pending = wait(pending, return_when=FIRST_EXCEPTION)
            failed_required = [
                (futures[future], future.exception())
                for future in done
                if future.exception() is not None
                and futures[future].name not in optional_names
            ]
            if failed_required:
                for remaining in pending:
                    remaining.cancel()
                query, error = min(failed_required, key=lambda item: item[0].ordinal)
                raise DashboardQueryError(query.name) from error
            for future in done:
                query = futures[future]
                error = future.exception()
                if error is None:
                    name, rows = future.result()
                    results[name] = rows
                    continue
                if query.name in optional_names:
                    failed_optional.append(query.name)
                    continue

        ordered = {
            query.name: results[query.name]
            for query in all_queries
            if query.name in results
        }
        return ordered, tuple(sorted(failed_optional))

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
        result["chat"]["service_share"] = _chat_service_share(result["chat"]["trend"])
        for row in result["chat"]["by_user_service"]:
            row["service_category"] = self.service_category(row.get("service_id"))
        for row in result["auth"]["trend"] + result["auth"]["by_type"]:
            row["label"] = self.AUTH_LABELS.get(str(row.get("type_code")), "기타 인증 이벤트")
        for row in result["api"]["by_endpoint"]:
            method, path = split_usage_endpoint(str(row.get("endpoint") or ""))
            row["endpoint_label"] = endpoint_feature_label(method, path)
        serialized_filters = asdict(filters)
        if not filters.user_ids:
            serialized_filters.pop("user_ids")
        serialized_filters.pop("excluded_user_ids")
        payload = {
            "filters": serialized_filters,
            "limits": {"max_days": MAX_RANGE_DAYS, "cache_ttl_seconds": self._cache.ttl_seconds},
            "data_quality": _data_quality(result),
            **result,
        }
        self._cache.put(filters.cache_key(), payload)
        return payload


def comparison_window(
    filters: UsageFilters,
    state: ChatMaterializationState,
) -> tuple[date, date] | None:
    days = (filters.date_to - filters.date_from).days + 1
    previous_to = filters.date_from - timedelta(days=1)
    previous_from = previous_to - timedelta(days=days - 1)
    if previous_from < state.coverage_start:
        return None
    return previous_from, previous_to


def build_usage_comparison(
    result: dict[str, Any],
    filters: UsageFilters,
    *,
    previous_window: tuple[date, date] | None,
    previous_rows: dict[str, list[dict[str, Any]]] | None,
    unavailable_reason: str | None = None,
) -> dict[str, Any]:
    current_period = {
        "date_from": filters.date_from.isoformat(),
        "date_to": filters.date_to.isoformat(),
    }
    if previous_window is None or unavailable_reason is not None:
        return {
            "available": False,
            "reason": unavailable_reason or "previous_period_outside_coverage",
            "current_period": current_period,
            "previous_period": None,
            "metrics": {},
        }

    current_values = {
        "api_calls": sum(int(row.get("total_calls") or 0) for row in result["api"]["trend"]),
        "chat_sessions": sum(
            int(row.get("sessions") or 0)
            for row in result["chat"]["trend"]
            if row.get("service_id") in {61, 91, 94}
        ),
        "successful_logins": sum(
            int(row.get("events") or 0)
            for row in result["auth"]["by_type"]
            if row.get("type_code") == "AT0001"
        ),
        "credits": sum(float(row.get("credits") or 0) for row in result["credit"]["trend"]),
        "successful_downloads": sum(
            int(row.get("successful_downloads") or 0)
            for row in result["reports"]["trend"]
        ),
    }
    row_names = {
        "api_calls": "comparison_api",
        "chat_sessions": "comparison_chat_sessions",
        "successful_logins": "comparison_successful_logins",
        "credits": "comparison_credits",
        "successful_downloads": "comparison_successful_downloads",
    }
    metrics: dict[str, dict[str, int | float | None]] = {}
    for metric, current in current_values.items():
        rows = (previous_rows or {}).get(row_names[metric]) or [{"value": 0}]
        previous = rows[0].get("value") or 0
        change = None if float(previous) == 0 else round((float(current) - float(previous)) / float(previous) * 100, 2)
        metrics[metric] = {
            "current": current,
            "previous": previous,
            "change_rate_percent": change,
        }
    return {
        "available": True,
        "reason": None,
        "current_period": current_period,
        "previous_period": {
            "date_from": previous_window[0].isoformat(),
            "date_to": previous_window[1].isoformat(),
        },
        "metrics": metrics,
    }


def _chat_turn_item(row: dict[str, Any]) -> dict[str, Any]:
    detail_key = (
        f"rnd-session:{row['conversation_id']}"
        if row.get("source") == "rnd" and row.get("conversation_id")
        else row.get("detail_key") or row.get("trace_id")
    )
    if not detail_key and row.get("conversation_log_id") is not None:
        detail_key = f"conversation-log:{row['conversation_log_id']}"
    if not detail_key:
        raise ValueError("chat turn detail identity is absent")
    return {
        "turn_id": encode_chat_turn_id(str(detail_key)),
        "user_id": row["user_id"],
        "user_name": row.get("user_name"),
        "department": row.get("department"),
        "created_at": row["created_at"],
        "service_id": row["service_id"],
        "service_category": UsageStatsService.service_category(row.get("service_id")),
        "turn_index": row.get("turn_index"),
        "contract_status": row.get("contract_status"),
        "quality_label": row.get("quality_label"),
        "elapsed_ms": row.get("elapsed_ms"),
        "input_tokens": row.get("input_tokens"),
        "output_tokens": row.get("output_tokens"),
        "total_tokens": row.get("total_tokens"),
        "question_text": row.get("question_text"),
    }


def _user_filter(filters: UsageFilters) -> tuple[str, tuple[Any, ...]]:
    clauses: list[str] = ["(u.id IS NULL OR LOWER(u.user_id) NOT LIKE '%%test%%')"]
    params: list[Any] = []
    if filters.user_id is not None:
        clauses.append("u.id=%s")
        params.append(filters.user_id)
    if filters.user_ids:
        clauses.append(f"u.id IN ({','.join(['%s'] * len(filters.user_ids))})")
        params.extend(filters.user_ids)
    if filters.excluded_user_ids:
        clauses.append(f"(u.id IS NULL OR u.id NOT IN ({','.join(['%s'] * len(filters.excluded_user_ids))}))")
        params.extend(filters.excluded_user_ids)
    if filters.department:
        clauses.append("u.department=%s")
        params.append(filters.department)
    return ((" AND " + " AND ".join(clauses)) if clauses else "", tuple(params))


def _api_filter(filters: UsageFilters) -> tuple[str, tuple[Any, ...]]:
    clauses: list[str] = [
        "(actor_uid IS NULL OR actor_uid NOT IN "
        "(SELECT CONCAT('genos-user:', id) FROM dashboard_user_directory_v "
        "WHERE LOWER(user_id) LIKE '%%test%%'))"
    ]
    params: list[Any] = []
    if filters.user_id is not None:
        clauses.append("actor_uid=%s")
        params.append(f"genos-user:{filters.user_id}")
    if filters.user_ids:
        clauses.append(f"actor_uid IN ({','.join(['%s'] * len(filters.user_ids))})")
        params.extend(f"genos-user:{user_id}" for user_id in filters.user_ids)
    if filters.excluded_user_ids:
        clauses.append(
            f"(actor_uid IS NULL OR actor_uid NOT IN ({','.join(['%s'] * len(filters.excluded_user_ids))}))"
        )
        params.extend(f"genos-user:{user_id}" for user_id in filters.excluded_user_ids)
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


def endpoint_feature_label(method: str, path: str) -> str:
    return _ENDPOINT_LABELS.get(f"{method.upper()} {path}", UNKNOWN_ENDPOINT_LABEL)


def split_usage_endpoint(endpoint: str) -> tuple[str, str]:
    method, separator, path = endpoint.partition(" ")
    if not separator or not method or not path:
        return "UNKNOWN", endpoint
    return method, path


def _usage_log_query_parts(filters: UsageLogFilters) -> tuple[list[str], tuple[Any, ...]]:
    clauses: list[str] = []
    params: list[Any] = [
        filters.date_from.isoformat(),
        (filters.date_to + timedelta(days=1)).isoformat(),
    ]
    if filters.user_id is not None:
        clauses.append("u.id=%s")
        params.append(filters.user_id)
    if filters.user_ids:
        clauses.append(f"u.id IN ({','.join(['%s'] * len(filters.user_ids))})")
        params.extend(filters.user_ids)
    clauses.append("(u.id IS NULL OR LOWER(u.user_id) NOT LIKE '%%test%%')")
    if filters.excluded_user_ids:
        clauses.append(
            f"(u.id IS NULL OR u.id NOT IN ({','.join(['%s'] * len(filters.excluded_user_ids))}))"
        )
        params.extend(filters.excluded_user_ids)
    if filters.department:
        clauses.append("u.department=%s")
        params.append(filters.department)
    if filters.endpoint:
        clauses.append("a.endpoint=%s")
        params.append(filters.endpoint)
    if filters.http_status is not None:
        clauses.append("a.http_status=%s")
        params.append(filters.http_status)
    if filters.cursor is not None:
        clauses.append("(a.called_at < %s OR (a.called_at = %s AND a.id < %s))")
        params.extend((filters.cursor.called_at, filters.cursor.called_at, filters.cursor.id))
    return clauses, tuple(params)


def _chat_turn_query_parts(filters: ChatTurnFilters) -> tuple[list[str], tuple[Any, ...]]:
    clauses: list[str] = []
    params: list[Any] = [
        filters.date_from.isoformat(),
        (filters.date_to + timedelta(days=1)).isoformat(),
    ]
    if filters.user_id is not None:
        clauses.append("u.id=%s")
        params.append(filters.user_id)
    if filters.user_ids:
        clauses.append(f"u.id IN ({','.join(['%s'] * len(filters.user_ids))})")
        params.extend(filters.user_ids)
    clauses.append("LOWER(u.user_id) NOT LIKE '%%test%%'")
    if filters.excluded_user_ids:
        clauses.append(f"u.id NOT IN ({','.join(['%s'] * len(filters.excluded_user_ids))})")
        params.extend(filters.excluded_user_ids)
    if filters.department:
        clauses.append("u.department=%s")
        params.append(filters.department)
    if filters.cursor is not None:
        clauses.append(
            "(c.created_at < %s OR "
            f"(c.created_at = %s AND ({_CHAT_TURN_SOURCE_SQL} < %s OR "
            f"({_CHAT_TURN_SOURCE_SQL} = %s AND {_CHAT_TURN_ID_SQL} < %s))))"
        )
        params.extend(
            (
                filters.cursor.created_at,
                filters.cursor.created_at,
                filters.cursor.source,
                filters.cursor.source,
                filters.cursor.source_turn_id,
            )
        )
    return clauses, tuple(params)


def _and_clauses(clauses: list[str]) -> str:
    return (" AND " + " AND ".join(clauses)) if clauses else ""


def _request_options(value: Any) -> dict[str, Any]:
    raw = _request_options_raw(value)
    if not raw:
        return {}
    sanitized: dict[str, Any] = {}
    path = _allowed_child_options(raw.get("path"), _REQUEST_OPTIONS_ALLOWED["path"])
    if path:
        sanitized["path"] = path
    query = _allowed_child_options(raw.get("query"), _REQUEST_OPTIONS_ALLOWED["query"])
    if query:
        sanitized["query"] = query
    body = _allowed_child_options(raw.get("body"), _REQUEST_OPTIONS_ALLOWED["body"])
    raw_body = raw.get("body")
    if isinstance(raw_body, dict):
        filters = _allowed_child_options(
            raw_body.get("filters"),
            _REQUEST_OPTIONS_ALLOWED["body.filters"],
        )
        if filters:
            body["filters"] = filters
        options = _allowed_child_options(
            raw_body.get("options"),
            _REQUEST_OPTIONS_ALLOWED["body.options"],
        )
        if options:
            body["options"] = options
    if body:
        sanitized["body"] = body
    return sanitized


def _request_options_raw(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _allowed_child_options(value: Any, allowed_keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: child
        for key in allowed_keys
        if (child := value.get(key)) not in (None, {}, [])
    }


def _total_count(row: dict[str, Any] | None) -> int:
    if not row:
        return 0
    return int(row.get("total_count") or 0)


def _total_pages(total_count: int, page_size: int) -> int:
    if total_count == 0:
        return 0
    return (total_count + page_size - 1) // page_size


def _data_quality(result: dict[str, Any]) -> dict[str, int | float]:
    api_total = sum(int(row.get("total_calls") or 0) for row in result["api"]["trend"])
    api_attributed = sum(int(row.get("attributed_calls") or 0) for row in result["api"]["trend"])
    chat_total = sum(int(row.get("turns") or 0) for row in result["chat"]["trend"])
    chat_attributed = sum(int(row.get("attributed_turns") or 0) for row in result["chat"]["trend"])
    chat_elapsed_available = sum(
        int(row.get("elapsed_available_turns") or 0) for row in result["chat"]["trend"]
    )
    chat_token_available = sum(
        int(row.get("token_usage_available_turns") or 0)
        for row in result["chat"]["trend"]
    )
    chat_service_linked = sum(
        int(row.get("turns") or 0)
        for row in result["chat"]["trend"]
        if row.get("service_id") is not None
    )
    credit_total = sum(int(row.get("events") or 0) for row in result["credit"]["trend"])
    credit_attributed = sum(
        int(row.get("attributed_events") or 0) for row in result["credit"]["trend"]
    )
    return {
        "api_total_calls": api_total,
        "api_attributed_calls": api_attributed,
        "api_unknown_calls": max(api_total - api_attributed, 0),
        "api_attribution_rate": round(api_attributed / api_total, 4) if api_total else 0,
        "chat_attributed_turns": chat_attributed,
        "chat_unknown_turns": max(chat_total - chat_attributed, 0),
        "chat_service_linked_turns": chat_service_linked,
        "chat_service_linkage_missing_turns": max(chat_total - chat_service_linked, 0),
        "chat_elapsed_available_turns": chat_elapsed_available,
        "chat_token_usage_available_turns": chat_token_available,
        "credit_attributed_events": credit_attributed,
        "credit_unknown_events": max(credit_total - credit_attributed, 0),
    }


def _chat_service_share(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turns_by_category: dict[str, int] = {}
    for row in rows:
        service_category = UsageStatsService.service_category(row.get("service_id"))
        if service_category == "unknown":
            continue
        turns_by_category[service_category] = turns_by_category.get(service_category, 0) + int(
            row.get("turns") or 0
        )
    denominator = sum(turns_by_category.values())
    if denominator == 0:
        return []
    return [
        {
            "service_category": service_category,
            "turns": turns,
            "share": turns / denominator,
        }
        for service_category, turns in sorted(
            turns_by_category.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
