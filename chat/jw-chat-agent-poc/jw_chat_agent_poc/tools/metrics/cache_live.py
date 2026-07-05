from __future__ import annotations

from dataclasses import dataclass
import json
import os
import time
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class CacheSnapshot:
    cache_brands: list[dict[str, Any]]
    market_status: dict[str, Any]
    loaded_at: float


class MetricsCacheReader(Protocol):
    def load(self) -> CacheSnapshot: ...


@dataclass(frozen=True, slots=True)
class CausePayloadKey:
    brand: str
    view_type: str
    source: str
    measure: str
    market_id: str


@dataclass(frozen=True, slots=True)
class CausePayload:
    key: CausePayloadKey
    payload: dict[str, Any]
    loaded_at: float


class CausePayloadReader(Protocol):
    def load(self, key: CausePayloadKey) -> CausePayload: ...


@dataclass(frozen=True, slots=True)
class StaticMetricsCacheReader:
    cache_brands: list[dict[str, Any]]
    market_status: dict[str, Any]

    def load(self) -> CacheSnapshot:
        return CacheSnapshot(
            cache_brands=self.cache_brands,
            market_status=self.market_status,
            loaded_at=time.monotonic(),
        )


@dataclass(frozen=True, slots=True)
class StaticCausePayloadReader:
    payloads: dict[tuple[str, str, str, str, str], dict[str, Any]]

    def load(self, key: CausePayloadKey) -> CausePayload:
        payload = self.payloads.get((key.brand, key.view_type, key.source, key.measure, key.market_id))
        if payload is None:
            raise LookupError(f"cache_cause fixture is missing: {key}")
        return CausePayload(key=key, payload=payload, loaded_at=time.monotonic())


@dataclass(frozen=True, slots=True)
class MariaDbMetricsCacheReader:
    host: str = os.environ.get("CHAT_CACHE_DB_HOST", "llmops-mariadb-service.llmops.svc.cluster.local")
    port: int = int(os.environ.get("CHAT_CACHE_DB_PORT", "3306"))
    database: str = os.environ.get("CHAT_CACHE_DB_NAME", "jw_mart")
    user: str = os.environ.get("CHAT_CACHE_DB_USER", "llmops")
    password: str = os.environ.get("CHAT_CACHE_DB_PASSWORD", "")
    connect_timeout_s: int = int(os.environ.get("CHAT_CACHE_DB_CONNECT_TIMEOUT_S", "3"))
    read_timeout_s: int = int(os.environ.get("CHAT_CACHE_DB_READ_TIMEOUT_S", "5"))

    def load(self) -> CacheSnapshot:
        import pymysql

        with pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            connect_timeout=self.connect_timeout_s,
            read_timeout=self.read_timeout_s,
            write_timeout=self.read_timeout_s,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT response_json FROM cache_brands WHERE query_key=%s LIMIT 1", ("default",))
                brands_row = cursor.fetchone()
                cursor.execute("SELECT response_json FROM cache_market_status WHERE query_key=%s LIMIT 1", ("default",))
                status_row = cursor.fetchone()

        if not brands_row or not status_row:
            raise LookupError("cache_brands/cache_market_status default rows are missing")

        brands = json.loads(str(brands_row["response_json"]))
        status = json.loads(str(status_row["response_json"]))
        if not isinstance(brands, list):
            raise TypeError("cache_brands.response_json must be a JSON list")
        if not isinstance(status, dict):
            raise TypeError("cache_market_status.response_json must be a JSON object")

        return CacheSnapshot(cache_brands=brands, market_status=status, loaded_at=time.monotonic())


@dataclass(frozen=True, slots=True)
class MariaDbCausePayloadReader:
    host: str = os.environ.get("CHAT_CACHE_DB_HOST", "llmops-mariadb-service.llmops.svc.cluster.local")
    port: int = int(os.environ.get("CHAT_CACHE_DB_PORT", "3306"))
    database: str = os.environ.get("CHAT_CACHE_DB_NAME", "jw_mart")
    user: str = os.environ.get("CHAT_CACHE_DB_USER", "llmops")
    password: str = os.environ.get("CHAT_CACHE_DB_PASSWORD", "")
    connect_timeout_s: int = int(os.environ.get("CHAT_CACHE_DB_CONNECT_TIMEOUT_S", "3"))
    read_timeout_s: int = int(os.environ.get("CHAT_CAUSE_DB_READ_TIMEOUT_S", "15"))

    def load(self, key: CausePayloadKey) -> CausePayload:
        import pymysql

        with pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            connect_timeout=self.connect_timeout_s,
            read_timeout=self.read_timeout_s,
            write_timeout=self.read_timeout_s,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT response_json
                    FROM cache_cause
                    WHERE brand=%s
                      AND view_type=%s
                      AND source=%s
                      AND measure=%s
                      AND market_id=%s
                    LIMIT 1
                    """,
                    (key.brand, key.view_type, key.source, key.measure, key.market_id),
                )
                row = cursor.fetchone()

        if not row:
            raise LookupError(f"cache_cause row is missing: {key}")

        payload = json.loads(str(row["response_json"]))
        if not isinstance(payload, dict):
            raise TypeError("cache_cause.response_json must be a JSON object")
        return CausePayload(key=key, payload=payload, loaded_at=time.monotonic())


class TtlMetricsCache:
    def __init__(self, reader: MetricsCacheReader, ttl_seconds: int) -> None:
        self._reader = reader
        self._ttl_seconds = ttl_seconds
        self._snapshot: CacheSnapshot | None = None

    def snapshot(self) -> CacheSnapshot:
        current = self._snapshot
        now = time.monotonic()
        if current is None or now - current.loaded_at > self._ttl_seconds:
            current = self._reader.load()
            self._snapshot = current
        return current


class TtlCausePayloadCache:
    def __init__(self, reader: CausePayloadReader, ttl_seconds: int) -> None:
        self._reader = reader
        self._ttl_seconds = ttl_seconds
        self._payloads: dict[CausePayloadKey, CausePayload] = {}

    def payload(self, key: CausePayloadKey) -> CausePayload:
        current = self._payloads.get(key)
        now = time.monotonic()
        if current is None or now - current.loaded_at > self._ttl_seconds:
            current = self._reader.load(key)
            self._payloads[key] = current
        return current
