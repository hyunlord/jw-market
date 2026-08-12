from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import Any, Mapping

import pymysql


LOGGER = logging.getLogger(__name__)
STATE_TABLE = "agent_session_state"
SCHEMA_SQL = f"""CREATE TABLE {STATE_TABLE} (
  session_id VARCHAR(64) PRIMARY KEY,
  state_json JSON NOT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_updated (updated_at)
)"""


@dataclass(frozen=True, slots=True)
class SessionState:
    canonical_entities: tuple[str, ...] = ()
    primary_entity: str | None = None
    mentioned_related_entities: tuple[str, ...] = ()
    record_type: str | None = None
    status_filter: tuple[str, ...] = ()
    country_filter: tuple[str, ...] = ()
    requested_grain: str | None = None
    referenced_entity_set: tuple[str, ...] = ()
    active_filters: tuple[str, ...] = ()
    time_window: tuple[str, ...] = ()
    comparison_anchor: str | None = None
    last_numeric_facts: tuple[Mapping[str, Any], ...] = ()
    last_source_record_ids: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_value(cls, value: object) -> "SessionState":
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, Mapping):
            return cls()
        return cls(
            canonical_entities=_strings(value.get("canonical_entities")),
            primary_entity=_optional_text(value.get("primary_entity")),
            mentioned_related_entities=_strings(
                value.get("mentioned_related_entities")
            ),
            record_type=_optional_text(value.get("record_type")),
            status_filter=_strings(value.get("status_filter")),
            country_filter=_strings(value.get("country_filter")),
            requested_grain=_optional_text(value.get("requested_grain")),
            referenced_entity_set=_strings(value.get("referenced_entity_set")),
            active_filters=_strings(value.get("active_filters")),
            time_window=_strings(value.get("time_window")),
            comparison_anchor=_optional_text(value.get("comparison_anchor")),
            last_numeric_facts=tuple(
                dict(item)
                for item in value.get("last_numeric_facts", ())
                if isinstance(item, Mapping)
            )[:64],
            last_source_record_ids=_strings(value.get("last_source_record_ids"))[:64],
        )


@dataclass(frozen=True, slots=True)
class _Config:
    host: str
    port: int
    database: str
    user: str
    password: str


class SessionStateStore:
    """Small fail-open store for the approved V4 session-state table."""

    def __init__(self, config: object | None = None) -> None:
        self._config = config if config is not None else _config_from_env()

    @classmethod
    def from_env(cls) -> "SessionStateStore":
        return cls()

    def load(self, session_id: str) -> SessionState | None:
        if self._config is None or not session_id.strip():
            return None
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""SELECT state_json FROM {STATE_TABLE}
                        WHERE session_id = %s
                          AND updated_at >= UTC_TIMESTAMP() - INTERVAL 7 DAY
                        LIMIT 1""",
                        (session_id,),
                    )
                    row = cursor.fetchone()
            return SessionState.from_value(row[0]) if row else None
        except Exception as exc:  # noqa: BLE001 - state must never block an answer
            LOGGER.warning("v4 session state load failed error_type=%s", type(exc).__name__)
            return None

    def save(self, session_id: str, state: SessionState) -> None:
        if self._config is None or not session_id.strip():
            return
        try:
            payload = json.dumps(state.public_dict(), ensure_ascii=False, default=str)
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""INSERT INTO {STATE_TABLE} (session_id, state_json)
                        VALUES (%s, %s)
                        ON DUPLICATE KEY UPDATE state_json = VALUES(state_json)""",
                        (session_id, payload),
                    )
                connection.commit()
        except Exception as exc:  # noqa: BLE001 - state must never block an answer
            LOGGER.warning("v4 session state save failed error_type=%s", type(exc).__name__)

    def _connect(self):
        config = self._config
        assert isinstance(config, _Config)
        return pymysql.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database=config.database,
            charset="utf8mb4",
            autocommit=False,
            connect_timeout=3,
            read_timeout=5,
            write_timeout=5,
        )


def _config_from_env() -> _Config | None:
    values = {
        "host": os.environ.get("CHAT_CACHE_DB_HOST", "").strip(),
        "database": os.environ.get("CHAT_CACHE_DB_NAME", "").strip(),
        "user": os.environ.get("CHAT_CACHE_DB_USER", "").strip(),
        "password": os.environ.get("CHAT_CACHE_DB_PASSWORD", ""),
    }
    if not all(values.values()):
        return None
    return _Config(
        host=values["host"],
        port=int(os.environ.get("CHAT_CACHE_DB_PORT", "3306")),
        database=values["database"],
        user=values["user"],
        password=values["password"],
    )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
