"""Persistent on-demand cache and miss backpressure for general dynamic markets."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import base64
import binascii
import hashlib
import json
import logging
import math
from numbers import Real
import threading
import time
from typing import Any, Literal, Protocol
from uuid import uuid4
import zlib

from pymysql import MySQLError

from pipeline.scripts.api import db


logger = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION = "dynamic-market-response-v5-source-windowed-growth-company-rankings"
CACHE_SOURCE_POLICY_VERSION = "cause-build-response-20260714-f064-filter-complete-v2"
CACHE_ENCODING = "zlib-base64"
DEFAULT_TTL_SECONDS = 86_400
DEFAULT_LEASE_SECONDS = 120
DEFAULT_MAX_ROWS = 1_000
DEFAULT_MAX_BYTES = 1_073_741_824
DEFAULT_MAX_ENTRY_BYTES = 8 * 1024 * 1024
DEFAULT_HIGH_WATER_RATIO = 0.90
DEFAULT_LOW_WATER_RATIO = 0.75
DEFAULT_PRUNE_GRACE_SECONDS = 300
DEFAULT_PRUNE_BATCH_LIMIT = 100
_BUILD_SEMAPHORE = threading.BoundedSemaphore(3)
PersistenceScheduler = Callable[[Callable[[], None]], None]


class DynamicMarketOverloadedError(RuntimeError):
    """Raised when all distinct dynamic-market miss slots are occupied."""


class DynamicResponseCacheUnavailable(RuntimeError):
    """Raised when the optional cache table cannot be accessed."""


@dataclass(frozen=True, slots=True)
class CacheClaim:
    action: Literal["hit", "wait", "build"]
    response_json: str | None = None
    lease_owner: str | None = None

    @classmethod
    def hit(cls, response_json: str) -> "CacheClaim":
        return cls("hit", response_json=response_json)

    @classmethod
    def wait(cls) -> "CacheClaim":
        return cls("wait")

    @classmethod
    def build(cls, lease_owner: str) -> "CacheClaim":
        return cls("build", lease_owner=lease_owner)


@dataclass(frozen=True, slots=True)
class CachePruneResult:
    total_rows: int
    total_bytes: int
    candidates: int
    selected: int
    deleted: int
    deleted_bytes: int


class DynamicResponseCacheStore(Protocol):
    def source_epoch(self) -> str: ...

    def claim(self, *, cache_key: str, request_json: str, source_epoch: str) -> CacheClaim: ...

    def complete(self, *, cache_key: str, lease_owner: str, source_epoch: str, response_json: str) -> None: ...

    def fail(
        self,
        *,
        cache_key: str,
        lease_owner: str,
        failure_reason: str,
        last_error: str | None = None,
    ) -> None: ...


def canonical_request_json(request: Mapping[str, Any]) -> str:
    return json.dumps(_canonicalize(request), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_json_value(
    value: Any,
    *,
    on_non_finite: Callable[[str], None] | None = None,
    path: str = "$",
) -> Any:
    """Return a JSON-safe value, replacing every non-finite number with null."""

    if isinstance(value, Mapping):
        return {
            str(key): normalize_json_value(item, on_non_finite=on_non_finite, path=f"{path}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            normalize_json_value(item, on_non_finite=on_non_finite, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    candidate = value.item() if hasattr(value, "item") and callable(value.item) else value
    if isinstance(candidate, (Real, Decimal)) and not isinstance(candidate, (bool, int)) and not math.isfinite(float(candidate)):
        if on_non_finite is not None:
            on_non_finite(path)
        return None
    return candidate


def select_prune_keys(
    rows: list[dict[str, Any]],
    *,
    total_rows: int,
    total_bytes: int,
    max_rows: int,
    max_bytes: int,
    batch_limit: int,
    high_water_ratio: float = DEFAULT_HIGH_WATER_RATIO,
    low_water_ratio: float = DEFAULT_LOW_WATER_RATIO,
) -> list[str]:
    """Select a bounded prune batch without locking the cache namespace."""

    eligible = [row for row in rows if row.get("state") != "building"]

    def prune_priority(row: dict[str, Any]) -> tuple[Any, ...]:
        last_used = row.get("last_used")
        if last_used is None:
            last_used = datetime.min
        hit_count = int(row.get("hit_count") or 0)
        if row.get("expired") or row.get("state") == "failed":
            return (0, last_used, str(row.get("cache_key") or ""))
        if hit_count == 0:
            return (1, last_used, str(row.get("cache_key") or ""))
        return (2, last_used, hit_count, str(row.get("cache_key") or ""))

    ordered = sorted(eligible, key=prune_priority)
    selected: list[str] = []
    remaining_rows = total_rows
    remaining_bytes = total_bytes

    for row in ordered:
        if len(selected) >= batch_limit:
            break
        if not (row.get("expired") or row.get("state") == "failed"):
            continue
        selected.append(str(row["cache_key"]))
        remaining_rows -= 1
        remaining_bytes -= int(row.get("payload_size") or 0)

    above_high_water = (
        total_rows > int(max_rows * high_water_ratio)
        or total_bytes > int(max_bytes * high_water_ratio)
    )
    if not above_high_water:
        return selected

    target_rows = int(max_rows * low_water_ratio)
    target_bytes = int(max_bytes * low_water_ratio)
    already_selected = set(selected)
    for row in ordered:
        if len(selected) >= batch_limit:
            break
        if remaining_rows <= target_rows and remaining_bytes <= target_bytes:
            break
        cache_key = str(row["cache_key"])
        if cache_key in already_selected:
            continue
        selected.append(cache_key)
        already_selected.add(cache_key)
        remaining_rows -= 1
        remaining_bytes -= int(row.get("payload_size") or 0)
    return selected


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        normalized = [_canonicalize(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    return value


class DynamicResponseCache:
    def __init__(
        self,
        *,
        store: DynamicResponseCacheStore,
        build_semaphore: threading.BoundedSemaphore = _BUILD_SEMAPHORE,
        poll_interval_seconds: float = 0.1,
        wait_timeout_seconds: float = 60.0,
        max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
    ) -> None:
        self._store = store
        self._build_semaphore = build_semaphore
        self._poll_interval_seconds = poll_interval_seconds
        self._wait_timeout_seconds = wait_timeout_seconds
        self._max_entry_bytes = max_entry_bytes

    def get_or_build(
        self,
        request: Mapping[str, Any],
        builder: Callable[[], dict[str, Any]],
        *,
        persistence_scheduler: PersistenceScheduler | None = None,
    ) -> dict[str, Any]:
        request_json = canonical_request_json(request)
        source_epoch = self._store.source_epoch()
        cache_key = hashlib.sha256(
            f"{CACHE_SCHEMA_VERSION}\n{source_epoch}\n{request_json}".encode("utf-8")
        ).hexdigest()
        deadline = time.monotonic() + self._wait_timeout_seconds
        while True:
            claim = self._store.claim(
                cache_key=cache_key,
                request_json=request_json,
                source_epoch=source_epoch,
            )
            if claim.action == "hit" and claim.response_json is not None:
                payload = json.loads(_decode_cached_response(claim.response_json))
                if not isinstance(payload, dict):
                    raise DynamicResponseCacheUnavailable("cached dynamic response is not an object")
                return payload
            if claim.action == "build" and claim.lease_owner is not None:
                return self._build(
                    cache_key=cache_key,
                    request_json=request_json,
                    source_epoch=source_epoch,
                    lease_owner=claim.lease_owner,
                    builder=builder,
                    persistence_scheduler=persistence_scheduler,
                )
            if time.monotonic() >= deadline:
                raise DynamicMarketOverloadedError("timed out waiting for an identical dynamic-market request")
            time.sleep(self._poll_interval_seconds)

    def _build(
        self,
        *,
        cache_key: str,
        request_json: str,
        source_epoch: str,
        lease_owner: str,
        builder: Callable[[], dict[str, Any]],
        persistence_scheduler: PersistenceScheduler | None,
    ) -> dict[str, Any]:
        if not self._build_semaphore.acquire(blocking=False):
            self._store.fail(
                cache_key=cache_key,
                lease_owner=lease_owner,
                failure_reason="overloaded",
                last_error="DynamicMarketOverloadedError: dynamic-market miss capacity is full",
            )
            raise DynamicMarketOverloadedError("dynamic-market miss capacity is full")
        try:
            non_finite_paths: list[str] = []
            payload = normalize_json_value(builder(), on_non_finite=non_finite_paths.append)
            if non_finite_paths:
                logger.warning(
                    "dynamic_response_non_finite_normalized request=%s paths=%s",
                    request_json,
                    non_finite_paths,
                )
            response_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            stored_response = _encode_cached_response(response_json)
            if len(stored_response.encode("utf-8")) > self._max_entry_bytes:
                self._store.fail(
                    cache_key=cache_key,
                    lease_owner=lease_owner,
                    failure_reason="entry_too_large",
                    last_error=(
                        "compressed response exceeds limit: "
                        f"{len(stored_response.encode('utf-8'))}>{self._max_entry_bytes}"
                    ),
                )
                logger.warning(
                    "dynamic_response_cache_entry_skipped request=%s compressed_bytes=%d limit=%d",
                    request_json,
                    len(stored_response.encode("utf-8")),
                    self._max_entry_bytes,
                )
                return payload
            def persist() -> None:
                try:
                    self._store.complete(
                        cache_key=cache_key,
                        lease_owner=lease_owner,
                        source_epoch=source_epoch,
                        response_json=stored_response,
                    )
                except DynamicResponseCacheUnavailable as exc:
                    self._store.fail(
                        cache_key=cache_key,
                        lease_owner=lease_owner,
                        failure_reason="persistence_error",
                        last_error=_error_summary(exc),
                    )
                    logger.warning("dynamic_response_cache_store_failed", exc_info=True)

            def persist_deferred() -> None:
                try:
                    persist()
                except Exception as exc:
                    self._store.fail(
                        cache_key=cache_key,
                        lease_owner=lease_owner,
                        failure_reason="persistence_bug",
                        last_error=_error_summary(exc),
                    )
                    logger.exception("dynamic_response_cache_store_bug")
                    raise

            if persistence_scheduler is None:
                persist()
            else:
                try:
                    persistence_scheduler(persist_deferred)
                except Exception:
                    logger.warning("dynamic_response_cache_schedule_failed", exc_info=True)
                    persist()
            return payload
        except Exception as exc:
            self._store.fail(
                cache_key=cache_key,
                lease_owner=lease_owner,
                failure_reason="builder_error",
                last_error=_error_summary(exc),
            )
            raise
        finally:
            self._build_semaphore.release()


def _error_summary(exc: Exception, *, limit: int = 4096) -> str:
    return f"{type(exc).__name__}: {exc}"[:limit]


def _encode_cached_response(response_json: str) -> str:
    compressed = zlib.compress(response_json.encode("utf-8"), level=1)
    return json.dumps(
        {
            "__cache_encoding": CACHE_ENCODING,
            "data": base64.b64encode(compressed).decode("ascii"),
        },
        separators=(",", ":"),
    )


def _decode_cached_response(stored_response: str) -> str:
    try:
        wrapper = json.loads(stored_response)
    except json.JSONDecodeError as exc:
        raise DynamicResponseCacheUnavailable("cached dynamic response is not valid JSON") from exc
    if not isinstance(wrapper, dict) or wrapper.get("__cache_encoding") != CACHE_ENCODING:
        return stored_response
    encoded = wrapper.get("data")
    if not isinstance(encoded, str):
        raise DynamicResponseCacheUnavailable("cached dynamic response encoding has no data")
    try:
        return zlib.decompress(base64.b64decode(encoded, validate=True)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, zlib.error) as exc:
        raise DynamicResponseCacheUnavailable("cached dynamic response encoding is invalid") from exc


class MySQLDynamicResponseCacheStore:
    def __init__(
        self,
        *,
        mart_db: str,
        general_dimension_db: str,
        strategic_dimension_db: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        namespace: str = "dynamic",
    ) -> None:
        self._mart_db = mart_db
        self._general_dimension_db = general_dimension_db
        self._strategic_dimension_db = strategic_dimension_db
        self._ttl_seconds = ttl_seconds
        self._lease_seconds = lease_seconds
        self._max_rows = max_rows
        self._max_bytes = max_bytes
        self._namespace = namespace
        self._epoch_lock = threading.Lock()
        self._epoch_cached: tuple[float, str] | None = None

    def source_epoch(self) -> str:
        with self._epoch_lock:
            if self._epoch_cached is not None and time.monotonic() - self._epoch_cached[0] < 5.0:
                return self._epoch_cached[1]
        try:
            mart_versions: list[dict[str, Any]] = []
            for table_name in (
                "mart_general_brand_metric",
                "mart_general_market_metric",
                "mart_strategic_ml_brand_metric",
                "mart_strategic_ml_market_metric",
                "mart_strategic_cd_brand_metric",
                "mart_strategic_cd_market_metric",
            ):
                mart_versions.extend(
                    db.fetch_all(
                        f"""
                        SELECT %s AS table_name, computation_version, computed_at
                        FROM `{self._mart_db}`.`{table_name}`
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (table_name,),
                    )
                )
            dimension_versions: list[dict[str, Any]] = []
            for schema, table_name, index_name in (
                (self._general_dimension_db, "mart_general_filter_dimension_metric", "idx_filter_option"),
                (self._strategic_dimension_db, "mart_strategic_filter_dimension_metric", "idx_options"),
            ):
                rows = db.fetch_all(
                    f"""
                    SELECT %s AS table_name, d.source, d.dimension_type,
                           (SELECT f.computed_at
                            FROM `{schema}`.`{table_name}` f FORCE INDEX (`{index_name}`)
                            WHERE f.source = d.source
                              AND f.dimension_type = d.dimension_type
                            ORDER BY f.dimension_value_hash
                            LIMIT 1) AS computed_at
                    FROM (
                        SELECT DISTINCT source, dimension_type
                        FROM `{schema}`.`{table_name}`
                    ) d
                    ORDER BY d.source, d.dimension_type
                    """,
                    (table_name,),
                )
                if not rows:
                    raise DynamicResponseCacheUnavailable(
                        f"filter dimension explicit data version is missing: {table_name}"
                    )
                dimension_versions.extend(rows)
            catalogs = db.fetch_all(
                f"""
                SELECT 'catalog_ml_market' AS table_name, MAX(source_file_version) AS source_file_version,
                       MAX(ingested_at) AS ingested_at,
                       MAX(catalog_manifest_hash) AS catalog_manifest_hash
                FROM `{self._mart_db}`.`catalog_ml_market`
                UNION ALL
                SELECT 'catalog_cd_market', MAX(source_file_version),
                       MAX(ingested_at), MAX(catalog_manifest_hash)
                FROM `{self._mart_db}`.`catalog_cd_market`
                UNION ALL
                SELECT 'catalog_strategic_brand', MAX(source_file_version),
                       MAX(ingested_at), MAX(catalog_manifest_hash)
                FROM `{self._mart_db}`.`catalog_strategic_brand`
                """,
                (),
            )
            event_versions = (
                db.fetch_all(
                    f"""
                    SELECT 'event_brand_scores' AS table_name,
                           workflow_id, catalog_version, generated_at
                    FROM `{self._mart_db}`.`event_brand_scores`
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (),
                )
                if self._namespace == "deep_expensive"
                else []
            )
        except MySQLError as exc:
            raise DynamicResponseCacheUnavailable("cannot read dynamic mart fingerprint") from exc
        if len(mart_versions) != 6 or len(catalogs) != 3:
            raise DynamicResponseCacheUnavailable("dynamic mart explicit data versions are incomplete")
        if self._namespace == "deep_expensive" and len(event_versions) != 1:
            raise DynamicResponseCacheUnavailable("deep event explicit data version is missing")
        fingerprint = [
            [
                str(row.get("table_name") or ""),
                str(row.get("computation_version") or ""),
                str(row.get("computed_at") or ""),
            ]
            for row in mart_versions
        ]
        fingerprint.extend(
            [
                str(row.get("table_name") or ""),
                str(row.get("source") or ""),
                str(row.get("dimension_type") or ""),
                str(row.get("computed_at") or ""),
            ]
            for row in dimension_versions
        )
        fingerprint.extend(
            [
                str(row.get("table_name") or ""),
                str(row.get("source_file_version") or ""),
                str(row.get("ingested_at") or ""),
                str(row.get("catalog_manifest_hash") or ""),
            ]
            for row in catalogs
        )
        fingerprint.extend(
            [
                str(row.get("table_name") or ""),
                str(row.get("workflow_id") or ""),
                str(row.get("catalog_version") or ""),
                str(row.get("generated_at") or ""),
            ]
            for row in event_versions
        )
        fingerprint.append(["namespace", self._namespace])
        fingerprint.append(["policy", CACHE_SOURCE_POLICY_VERSION])
        epoch = hashlib.sha256(
            json.dumps(fingerprint, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self._epoch_lock:
            self._epoch_cached = (time.monotonic(), epoch)
        return epoch

    def claim(self, *, cache_key: str, request_json: str, source_epoch: str) -> CacheClaim:
        now = datetime.now()
        owner = uuid4().hex
        try:
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cache_dynamic_market_response (
                        cache_key, namespace, request_json, source_epoch, state, lease_owner, lease_expires_at,
                        response_json, response_sha256, payload_size, expires_at, hit_count,
                        failure_reason, attempt_count, last_error, last_attempt_at, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, 'building', %s, %s, NULL, NULL, NULL, NULL, 0,
                        NULL, 1, NULL, %s, %s, %s
                    )
                    ON DUPLICATE KEY UPDATE cache_key = VALUES(cache_key)
                    """,
                    (
                        cache_key,
                        self._namespace,
                        request_json,
                        source_epoch,
                        owner,
                        now + timedelta(seconds=self._lease_seconds),
                        now,
                        now,
                        now,
                    ),
                )
                cur.execute(
                    """
                    SELECT state, source_epoch, response_json, expires_at, lease_owner, lease_expires_at
                    FROM cache_dynamic_market_response
                    WHERE cache_key = %s
                    FOR UPDATE
                    """,
                    (cache_key,),
                )
                row = cur.fetchone()
                if row and row.get("state") == "building" and row.get("lease_owner") == owner:
                    conn.commit()
                    return CacheClaim.build(owner)
                if row and row.get("state") == "ready" and row.get("source_epoch") == source_epoch:
                    expires_at = row.get("expires_at")
                    if expires_at is not None and expires_at > now and isinstance(row.get("response_json"), str):
                        cur.execute(
                            """
                            UPDATE cache_dynamic_market_response
                            SET hit_count = hit_count + 1, last_hit_at = %s
                            WHERE cache_key = %s
                            """,
                            (now, cache_key),
                        )
                        conn.commit()
                        return CacheClaim.hit(row["response_json"])
                if row and row.get("state") == "building" and row.get("source_epoch") == source_epoch:
                    lease_expires_at = row.get("lease_expires_at")
                    if lease_expires_at is not None and lease_expires_at > now:
                        conn.commit()
                        return CacheClaim.wait()
                cur.execute(
                    """
                    UPDATE cache_dynamic_market_response
                    SET request_json = %s, source_epoch = %s, state = 'building', lease_owner = %s,
                        lease_expires_at = %s, response_json = NULL, response_sha256 = NULL,
                        payload_size = NULL, expires_at = NULL, failure_reason = NULL,
                        attempt_count = attempt_count + 1, last_error = NULL,
                        last_attempt_at = %s, updated_at = %s
                    WHERE cache_key = %s
                    """,
                    (
                        request_json,
                        source_epoch,
                        owner,
                        now + timedelta(seconds=self._lease_seconds),
                        now,
                        now,
                        cache_key,
                    ),
                )
                conn.commit()
                return CacheClaim.build(owner)
        except MySQLError as exc:
            raise DynamicResponseCacheUnavailable("dynamic response cache table is unavailable") from exc

    def complete(self, *, cache_key: str, lease_owner: str, source_epoch: str, response_json: str) -> None:
        now = datetime.now()
        digest = hashlib.sha256(response_json.encode("utf-8")).hexdigest()
        payload_size = len(response_json.encode("utf-8"))
        try:
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE cache_dynamic_market_response
                    SET state = 'ready', source_epoch = %s, response_json = %s,
                        response_sha256 = %s, payload_size = %s, expires_at = %s,
                        lease_owner = NULL, lease_expires_at = NULL, failure_reason = NULL,
                        last_error = NULL, updated_at = %s
                    WHERE cache_key = %s AND state = 'building' AND lease_owner = %s
                    """,
                    (
                        source_epoch,
                        response_json,
                        digest,
                        payload_size,
                        now + timedelta(seconds=self._ttl_seconds),
                        now,
                        cache_key,
                        lease_owner,
                    ),
                )
                affected = cur.rowcount
                if affected != 1:
                    raise DynamicResponseCacheUnavailable("dynamic response cache lease was lost")
                conn.commit()
        except MySQLError as exc:
            raise DynamicResponseCacheUnavailable("cannot store dynamic response cache entry") from exc

    def prune(
        self,
        *,
        now: datetime | None = None,
        grace_seconds: int = DEFAULT_PRUNE_GRACE_SECONDS,
        batch_limit: int = DEFAULT_PRUNE_BATCH_LIMIT,
    ) -> CachePruneResult:
        prune_time = now or datetime.now()
        grace_cutoff = prune_time - timedelta(seconds=grace_seconds)
        try:
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) AS total_rows,
                           COALESCE(SUM(payload_size), 0) AS total_bytes
                    FROM cache_dynamic_market_response
                    WHERE namespace = %s
                    """,
                    (self._namespace,),
                )
                summary = cur.fetchone() or {}
                total_rows = int(summary.get("total_rows") or 0)
                total_bytes = int(summary.get("total_bytes") or 0)
                cur.execute(
                    """
                    SELECT cache_key, state, payload_size, hit_count, last_hit_at,
                           expires_at, updated_at,
                           expires_at <= %s AS expired,
                           COALESCE(last_hit_at, updated_at) AS last_used
                    FROM cache_dynamic_market_response
                    WHERE namespace = %s
                      AND state <> 'building'
                      AND updated_at <= %s
                    ORDER BY
                      CASE
                        WHEN state = 'failed' OR expires_at <= %s THEN 0
                        WHEN hit_count = 0 THEN 1
                        ELSE 2
                      END,
                      COALESCE(last_hit_at, updated_at), hit_count, cache_key
                    LIMIT %s
                    """,
                    (prune_time, self._namespace, grace_cutoff, prune_time, batch_limit),
                )
                candidates = list(cur.fetchall())
                selected_keys = select_prune_keys(
                    candidates,
                    total_rows=total_rows,
                    total_bytes=total_bytes,
                    max_rows=self._max_rows,
                    max_bytes=self._max_bytes,
                    batch_limit=batch_limit,
                )
                candidates_by_key = {str(row["cache_key"]): row for row in candidates}
                deleted = 0
                deleted_bytes = 0
                for cache_key in selected_keys:
                    observed = candidates_by_key[cache_key]
                    cur.execute(
                        """
                        DELETE FROM cache_dynamic_market_response
                        WHERE cache_key = %s AND namespace = %s AND state = %s
                          AND updated_at = %s AND hit_count = %s
                          AND last_hit_at <=> %s
                        """,
                        (
                            cache_key,
                            self._namespace,
                            observed.get("state"),
                            observed.get("updated_at"),
                            int(observed.get("hit_count") or 0),
                            observed.get("last_hit_at"),
                        ),
                    )
                    if cur.rowcount == 1:
                        deleted += 1
                        deleted_bytes += int(observed.get("payload_size") or 0)
                conn.commit()
                return CachePruneResult(
                    total_rows=total_rows,
                    total_bytes=total_bytes,
                    candidates=len(candidates),
                    selected=len(selected_keys),
                    deleted=deleted,
                    deleted_bytes=deleted_bytes,
                )
        except MySQLError as exc:
            raise DynamicResponseCacheUnavailable("cannot prune dynamic response cache") from exc

    def fail(
        self,
        *,
        cache_key: str,
        lease_owner: str,
        failure_reason: str,
        last_error: str | None = None,
    ) -> None:
        try:
            db.execute(
                """
                UPDATE cache_dynamic_market_response
                SET state = 'failed', lease_owner = NULL, lease_expires_at = NULL,
                    failure_reason = %s, last_error = %s, last_attempt_at = %s, updated_at = %s
                WHERE cache_key = %s AND state = 'building' AND lease_owner = %s
                """,
                (
                    failure_reason[:255],
                    last_error[:4096] if last_error is not None else None,
                    datetime.now(),
                    datetime.now(),
                    cache_key,
                    lease_owner,
                ),
            )
        except MySQLError:
            logger.warning("dynamic_response_cache_fail_mark_failed", exc_info=True)
