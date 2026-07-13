from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import json
import os
import re
import threading
import time
from typing import Any, Final, Protocol


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
class CsdActivityTarget:
    brand: str
    market: str
    master_product: str


@dataclass(frozen=True, slots=True)
class CsdActivityRow:
    period_ym: str
    product_details: int


@dataclass(frozen=True, slots=True)
class CsdActivityPayload:
    target: CsdActivityTarget
    rows: tuple[CsdActivityRow, ...]
    loaded_at: float


class CsdActivityReader(Protocol):
    def load(self, target: CsdActivityTarget, limit: int) -> CsdActivityPayload: ...


class CsdActivityTargetReader(Protocol):
    def load(self) -> tuple[CsdActivityTarget, ...]: ...


class CsdActivityTargetLoadError(RuntimeError):
    pass


_LEGACY_CSD_ACTIVITY_TARGETS: Final[tuple[CsdActivityTarget, ...]] = (
    CsdActivityTarget("리바로", "LIVALO Market", "LIVALO"),
    CsdActivityTarget("리바로젯", "LIVALOZET Market", "LIVALOZET"),
)


_CSD_ACTIVITY_MASTER_PRODUCT_ALIASES: Final[dict[str, str]] = {
    "가드렛": "GUARDLET",
    "가드메트": "GUARDMET",
    "제이클": "JCLE",
    "리바로": "LIVALO",
    "리바로젯": "LIVALOZET",
    "리바로페노": "LIVALOFENO",
    "리바로하이": "LIVALO HI",
    "리바로브이": "LIVALO V",
    "트루패스": "THRUPAS",
    "페린젝트": "FERINJECT",
    "포스레놀": "FOSRENOL",
    "가나칸": "GANAKHAN",
    "위너프": "WINUF",
    "위너프A+": "WINUF",
    "엔커버": "ENCOVER",
    "플라주오피": "PLAJU OP",
}


def csd_activity_target_for_brand(brand: str) -> CsdActivityTarget | None:
    return StaticCsdActivityTargetReader(_LEGACY_CSD_ACTIVITY_TARGETS).target_for_brand(brand)


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
class StaticCsdActivityReader:
    rows_by_target: dict[tuple[str, str], tuple[tuple[str, int], ...]]

    def load(self, target: CsdActivityTarget, limit: int) -> CsdActivityPayload:
        rows = self.rows_by_target.get((target.market, target.master_product), ())
        selected = rows[-limit:] if limit > 0 else rows
        return CsdActivityPayload(
            target=target,
            rows=tuple(CsdActivityRow(str(period), int(value)) for period, value in selected),
            loaded_at=time.monotonic(),
        )


@dataclass(frozen=True, slots=True)
class StaticCsdActivityTargetReader:
    targets: tuple[CsdActivityTarget, ...]

    def load(self) -> tuple[CsdActivityTarget, ...]:
        return self.targets

    def target_for_brand(self, brand: str) -> CsdActivityTarget | None:
        lookup = _targets_by_brand(self.targets)
        return lookup.get(_normalise_brand_name(brand))


@dataclass(frozen=True, slots=True)
class MariaDbCsdActivityTargetReader:
    host: str = os.environ.get("CHAT_CACHE_DB_HOST", "llmops-mariadb-service.llmops.svc.cluster.local")
    port: int = int(os.environ.get("CHAT_CACHE_DB_PORT", "3306"))
    database: str = os.environ.get("CHAT_CACHE_DB_NAME", "jw_mart")
    schema: str = os.environ.get("CHAT_CSD_ACTIVITY_SCHEMA", "jw_brand_activity_stage")
    user: str = os.environ.get("CHAT_CACHE_DB_USER", "llmops")
    password: str = os.environ.get("CHAT_CACHE_DB_PASSWORD", "")
    connect_timeout_s: int = int(os.environ.get("CHAT_CACHE_DB_CONNECT_TIMEOUT_S", "3"))
    read_timeout_s: int = int(os.environ.get("CHAT_CSD_ACTIVITY_DB_READ_TIMEOUT_S", "5"))

    def load(self) -> tuple[CsdActivityTarget, ...]:
        import pymysql

        schema = _quote_identifier(self.schema)
        try:
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
                        f"""
                        SELECT market, master_product,
                               SUM(COALESCE(product_details, 0)) AS total_activity
                        FROM {schema}.`csd_channel_dynamics_stage`
                        WHERE jw_channel = 'TOTAL'
                        GROUP BY market, master_product
                        """
                    )
                    rows = cursor.fetchall()
        except pymysql.MySQLError as exc:
            raise CsdActivityTargetLoadError("failed to load CSD activity targets") from exc

        by_product: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            product = _normalise_master_product(row.get("master_product"))
            if not product:
                continue
            by_product.setdefault(product, []).append(row)

        targets: list[CsdActivityTarget] = []
        for brand, master_product in _CSD_ACTIVITY_MASTER_PRODUCT_ALIASES.items():
            candidates = by_product.get(_normalise_master_product(master_product), [])
            best = _best_csd_activity_candidate(candidates)
            if best is None:
                continue
            targets.append(
                CsdActivityTarget(
                    brand=brand,
                    market=str(best["market"]),
                    master_product=str(best["master_product"]),
                )
            )
        if not targets:
            raise CsdActivityTargetLoadError("CSD activity target query returned no mapped brands")
        return tuple(targets)


@dataclass(frozen=True, slots=True)
class MariaDbMetricsCacheReader:
    host: str = field(default_factory=lambda: os.environ.get("CHAT_BRANDS_DB_HOST") or os.environ.get("CHAT_QUERY_DB_HOST") or os.environ.get("CHAT_CACHE_DB_HOST", "llmops-mariadb-service.llmops.svc.cluster.local"))
    port: int = field(default_factory=lambda: int(os.environ.get("CHAT_BRANDS_DB_PORT") or os.environ.get("CHAT_QUERY_DB_PORT") or os.environ.get("CHAT_CACHE_DB_PORT", "3306")))
    database: str = field(default_factory=lambda: os.environ.get("CHAT_BRANDS_DB_NAME") or os.environ.get("CHAT_QUERY_DB_NAME") or os.environ.get("CHAT_CACHE_DB_NAME", "jw_mart"))
    user: str = field(default_factory=lambda: os.environ.get("CHAT_BRANDS_DB_USER") or os.environ.get("CHAT_QUERY_DB_USER") or os.environ.get("CHAT_CACHE_DB_USER", "llmops"))
    password: str = field(default_factory=lambda: os.environ.get("CHAT_BRANDS_DB_PASSWORD") or os.environ.get("CHAT_QUERY_DB_PASSWORD") or os.environ.get("CHAT_CACHE_DB_PASSWORD", ""))
    connect_timeout_s: int = field(default_factory=lambda: int(os.environ.get("CHAT_BRANDS_DB_CONNECT_TIMEOUT_S") or os.environ.get("CHAT_QUERY_DB_CONNECT_TIMEOUT_S") or os.environ.get("CHAT_CACHE_DB_CONNECT_TIMEOUT_S", "3")))
    read_timeout_s: int = field(default_factory=lambda: int(os.environ.get("CHAT_BRANDS_DB_READ_TIMEOUT_S") or os.environ.get("CHAT_QUERY_DB_READ_TIMEOUT_S") or os.environ.get("CHAT_CACHE_DB_READ_TIMEOUT_S", "5")))

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

        if not brands_row:
            raise LookupError("cache_brands default row is missing")

        brands = json.loads(str(brands_row["response_json"]))
        if not isinstance(brands, list):
            raise TypeError("cache_brands.response_json must be a JSON list")

        return CacheSnapshot(cache_brands=brands, market_status={}, loaded_at=time.monotonic())


@dataclass(frozen=True, slots=True)
class UnavailableCausePayloadReader:
    """Compatibility reader that prevents legacy cause payload SQL reads."""

    def load(self, key: CausePayloadKey) -> CausePayload:
        raise LookupError(f"legacy cause payloads are disabled: {key}")


@dataclass(frozen=True, slots=True)
class MariaDbCsdActivityReader:
    host: str = os.environ.get("CHAT_CACHE_DB_HOST", "llmops-mariadb-service.llmops.svc.cluster.local")
    port: int = int(os.environ.get("CHAT_CACHE_DB_PORT", "3306"))
    database: str = os.environ.get("CHAT_CACHE_DB_NAME", "jw_mart")
    schema: str = os.environ.get("CHAT_CSD_ACTIVITY_SCHEMA", "jw_brand_activity_stage")
    user: str = os.environ.get("CHAT_CACHE_DB_USER", "llmops")
    password: str = os.environ.get("CHAT_CACHE_DB_PASSWORD", "")
    connect_timeout_s: int = int(os.environ.get("CHAT_CACHE_DB_CONNECT_TIMEOUT_S", "3"))
    read_timeout_s: int = int(os.environ.get("CHAT_CSD_ACTIVITY_DB_READ_TIMEOUT_S", "5"))

    def load(self, target: CsdActivityTarget, limit: int) -> CsdActivityPayload:
        import pymysql

        schema = _quote_identifier(self.schema)
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
                    f"""
                    SELECT period_ym, SUM(product_details) AS product_details
                    FROM {schema}.`csd_channel_dynamics_stage`
                    WHERE market = %s
                      AND master_product = %s
                      AND jw_channel = 'TOTAL'
                    GROUP BY period_ym
                    ORDER BY period_ym DESC
                    LIMIT %s
                    """,
                    (target.market, target.master_product, int(limit)),
                )
                rows = cursor.fetchall()

        parsed = tuple(
            CsdActivityRow(str(row["period_ym"]), int(row["product_details"] or 0))
            for row in reversed(rows)
        )
        return CsdActivityPayload(target=target, rows=parsed, loaded_at=time.monotonic())


PAYLOAD_CACHE_MAX_KEYS_ENV = "PAYLOAD_CACHE_MAX_KEYS"
# limits 10Gi 기준 역산: baseline ~1.1Gi + 동시 처리(N=3) 일시 피크 ~4.65Gi를 제외한
# 캐시 예산 ~3.1Gi를 키당 ~520Mi로 나눈 값.
DEFAULT_PAYLOAD_CACHE_MAX_KEYS = 6


def _payload_cache_max_keys() -> int:
    return max(1, int(os.environ.get(PAYLOAD_CACHE_MAX_KEYS_ENV, str(DEFAULT_PAYLOAD_CACHE_MAX_KEYS))))


class TtlMetricsCache:
    def __init__(self, reader: MetricsCacheReader, ttl_seconds: int) -> None:
        self._reader = reader
        self._ttl_seconds = ttl_seconds
        self._snapshot: CacheSnapshot | None = None
        self._load_lock = threading.Lock()

    def snapshot(self) -> CacheSnapshot:
        current = self._snapshot
        now = time.monotonic()
        if current is not None and now - current.loaded_at <= self._ttl_seconds:
            return current
        with self._load_lock:
            current = self._snapshot
            now = time.monotonic()
            if current is not None and now - current.loaded_at <= self._ttl_seconds:
                return current
            current = self._reader.load()
            self._snapshot = current
            return current


class TtlCausePayloadCache:
    def __init__(self, reader: CausePayloadReader, ttl_seconds: int, max_keys: int | None = None) -> None:
        self._reader = reader
        self._ttl_seconds = ttl_seconds
        self._max_keys = _payload_cache_max_keys() if max_keys is None else max(1, max_keys)
        self._payloads: OrderedDict[CausePayloadKey, CausePayload] = OrderedDict()
        self._state_lock = threading.Lock()
        self._key_locks: dict[CausePayloadKey, threading.Lock] = {}

    def payload(self, key: CausePayloadKey) -> CausePayload:
        current = self._fresh_payload(key)
        if current is not None:
            return current
        key_lock = self._lock_for(key)
        with key_lock:
            current = self._fresh_payload(key)
            if current is not None:
                return current
            loaded = self._reader.load(key)
            self._store(key, loaded)
            return loaded

    def _fresh_payload(self, key: CausePayloadKey) -> CausePayload | None:
        with self._state_lock:
            current = self._payloads.get(key)
            if current is None or time.monotonic() - current.loaded_at > self._ttl_seconds:
                return None
            self._payloads.move_to_end(key)
            return current

    def _lock_for(self, key: CausePayloadKey) -> threading.Lock:
        with self._state_lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def _store(self, key: CausePayloadKey, payload: CausePayload) -> None:
        with self._state_lock:
            self._payloads[key] = payload
            self._payloads.move_to_end(key)
            while len(self._payloads) > self._max_keys:
                evicted_key, _ = self._payloads.popitem(last=False)
                self._key_locks.pop(evicted_key, None)


class TtlCsdActivityCache:
    def __init__(self, reader: CsdActivityReader, ttl_seconds: int) -> None:
        self._reader = reader
        self._ttl_seconds = ttl_seconds
        self._payloads: dict[tuple[CsdActivityTarget, int], CsdActivityPayload] = {}

    def payload(self, target: CsdActivityTarget, limit: int) -> CsdActivityPayload:
        key = (target, limit)
        current = self._payloads.get(key)
        now = time.monotonic()
        if current is None or now - current.loaded_at > self._ttl_seconds:
            current = self._reader.load(target, limit)
            self._payloads[key] = current
        return current


class TtlCsdActivityTargetCache:
    def __init__(
        self,
        reader: CsdActivityTargetReader,
        ttl_seconds: int,
        fallback_targets: tuple[CsdActivityTarget, ...] = _LEGACY_CSD_ACTIVITY_TARGETS,
    ) -> None:
        self._reader = reader
        self._ttl_seconds = ttl_seconds
        self._fallback_targets = fallback_targets
        self._targets: tuple[CsdActivityTarget, ...] | None = None
        self._loaded_at = 0.0

    def target_for_brand(self, brand: str) -> CsdActivityTarget | None:
        lookup = _targets_by_brand(self._current_targets())
        return lookup.get(_normalise_brand_name(brand))

    def _current_targets(self) -> tuple[CsdActivityTarget, ...]:
        now = time.monotonic()
        if self._targets is None or now - self._loaded_at > self._ttl_seconds:
            try:
                self._targets = self._reader.load()
            except CsdActivityTargetLoadError:
                if self._targets is None:
                    self._targets = self._fallback_targets
            self._loaded_at = now
        return self._targets


_SHARED_CACHE_LOCK = threading.Lock()
_SHARED_METRICS_CACHES: dict[int, TtlMetricsCache] = {}
_SHARED_CAUSE_PAYLOAD_CACHES: dict[int, TtlCausePayloadCache] = {}


def shared_metrics_cache(ttl_seconds: int) -> TtlMetricsCache:
    """Process-wide snapshot cache so per-request tool instances share one load."""
    with _SHARED_CACHE_LOCK:
        cache = _SHARED_METRICS_CACHES.get(ttl_seconds)
        if cache is None:
            cache = TtlMetricsCache(MariaDbMetricsCacheReader(), ttl_seconds=ttl_seconds)
            _SHARED_METRICS_CACHES[ttl_seconds] = cache
        return cache


def shared_cause_payload_cache(ttl_seconds: int) -> TtlCausePayloadCache:
    """Compatibility cache that fails closed instead of querying legacy payloads."""
    with _SHARED_CACHE_LOCK:
        cache = _SHARED_CAUSE_PAYLOAD_CACHES.get(ttl_seconds)
        if cache is None:
            cache = TtlCausePayloadCache(UnavailableCausePayloadReader(), ttl_seconds=ttl_seconds)
            _SHARED_CAUSE_PAYLOAD_CACHES[ttl_seconds] = cache
        return cache


def _quote_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return f"`{value}`"


def _targets_by_brand(targets: tuple[CsdActivityTarget, ...]) -> dict[str, CsdActivityTarget]:
    return {_normalise_brand_name(target.brand): target for target in targets}


def _normalise_brand_name(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).casefold()


def _normalise_master_product(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _best_csd_activity_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    return max(candidates, key=lambda row: int(row.get("total_activity") or 0))
