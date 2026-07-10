"""Persistent on-demand cache and miss backpressure for general dynamic markets."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import base64
import binascii
import hashlib
import json
import logging
import threading
import time
from typing import Any, Literal, Protocol
from uuid import uuid4
import zlib

from pymysql import MySQLError

from pipeline.scripts.api import db


logger = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION = "dynamic-market-response-v1"
CACHE_ENCODING = "zlib-base64"
DEFAULT_TTL_SECONDS = 86_400
DEFAULT_LEASE_SECONDS = 120
_BUILD_SEMAPHORE = threading.BoundedSemaphore(3)


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


class DynamicResponseCacheStore(Protocol):
    def source_epoch(self) -> str: ...

    def claim(self, *, cache_key: str, request_json: str, source_epoch: str) -> CacheClaim: ...

    def complete(self, *, cache_key: str, lease_owner: str, source_epoch: str, response_json: str) -> None: ...

    def fail(self, *, cache_key: str, lease_owner: str) -> None: ...


def canonical_request_json(request: Mapping[str, Any]) -> str:
    return json.dumps(_canonicalize(request), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
    ) -> None:
        self._store = store
        self._build_semaphore = build_semaphore
        self._poll_interval_seconds = poll_interval_seconds
        self._wait_timeout_seconds = wait_timeout_seconds

    def get_or_build(self, request: Mapping[str, Any], builder: Callable[[], dict[str, Any]]) -> dict[str, Any]:
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
                    source_epoch=source_epoch,
                    lease_owner=claim.lease_owner,
                    builder=builder,
                )
            if time.monotonic() >= deadline:
                raise DynamicMarketOverloadedError("timed out waiting for an identical dynamic-market request")
            time.sleep(self._poll_interval_seconds)

    def _build(
        self,
        *,
        cache_key: str,
        source_epoch: str,
        lease_owner: str,
        builder: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        if not self._build_semaphore.acquire(blocking=False):
            self._store.fail(cache_key=cache_key, lease_owner=lease_owner)
            raise DynamicMarketOverloadedError("dynamic-market miss capacity is full")
        try:
            payload = builder()
            response_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            stored_response = _encode_cached_response(response_json)
            try:
                self._store.complete(
                    cache_key=cache_key,
                    lease_owner=lease_owner,
                    source_epoch=source_epoch,
                    response_json=stored_response,
                )
            except DynamicResponseCacheUnavailable:
                self._store.fail(cache_key=cache_key, lease_owner=lease_owner)
                logger.warning("dynamic_response_cache_store_failed", exc_info=True)
            return payload
        except Exception:
            self._store.fail(cache_key=cache_key, lease_owner=lease_owner)
            raise
        finally:
            self._build_semaphore.release()


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
        dimension_db: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._mart_db = mart_db
        self._dimension_db = dimension_db
        self._ttl_seconds = ttl_seconds
        self._lease_seconds = lease_seconds

    def source_epoch(self) -> str:
        try:
            rows = db.fetch_all(
                """
                SELECT TABLE_SCHEMA, TABLE_NAME, CREATE_TIME
                FROM information_schema.TABLES
                WHERE (TABLE_SCHEMA = %s AND TABLE_NAME IN ('mart_general_brand_metric', 'mart_general_market_metric'))
                   OR (TABLE_SCHEMA = %s AND TABLE_NAME = 'mart_general_filter_dimension_metric')
                ORDER BY TABLE_SCHEMA, TABLE_NAME
                """,
                (self._mart_db, self._dimension_db),
            )
        except MySQLError as exc:
            raise DynamicResponseCacheUnavailable("cannot read dynamic mart fingerprint") from exc
        fingerprint = [
            [str(row.get("TABLE_SCHEMA") or ""), str(row.get("TABLE_NAME") or ""), str(row.get("CREATE_TIME") or "")]
            for row in rows
        ]
        if not fingerprint:
            raise DynamicResponseCacheUnavailable("dynamic mart fingerprint tables are missing")
        return hashlib.sha256(
            json.dumps(fingerprint, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def claim(self, *, cache_key: str, request_json: str, source_epoch: str) -> CacheClaim:
        now = datetime.now()
        owner = uuid4().hex
        try:
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cache_dynamic_market_response (
                        cache_key, request_json, source_epoch, state, lease_owner, lease_expires_at,
                        response_json, response_sha256, payload_size, expires_at, hit_count,
                        created_at, updated_at
                    ) VALUES (%s, %s, %s, 'building', %s, %s, NULL, NULL, NULL, NULL, 0, %s, %s)
                    ON DUPLICATE KEY UPDATE cache_key = VALUES(cache_key)
                    """,
                    (
                        cache_key,
                        request_json,
                        source_epoch,
                        owner,
                        now + timedelta(seconds=self._lease_seconds),
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
                        payload_size = NULL, expires_at = NULL, updated_at = %s
                    WHERE cache_key = %s
                    """,
                    (
                        request_json,
                        source_epoch,
                        owner,
                        now + timedelta(seconds=self._lease_seconds),
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
        try:
            affected = db.execute(
                """
                UPDATE cache_dynamic_market_response
                SET state = 'ready', source_epoch = %s, response_json = %s,
                    response_sha256 = %s, payload_size = %s, expires_at = %s,
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = %s
                WHERE cache_key = %s AND state = 'building' AND lease_owner = %s
                """,
                (
                    source_epoch,
                    response_json,
                    digest,
                    len(response_json.encode("utf-8")),
                    now + timedelta(seconds=self._ttl_seconds),
                    now,
                    cache_key,
                    lease_owner,
                ),
            )
        except MySQLError as exc:
            raise DynamicResponseCacheUnavailable("cannot store dynamic response cache entry") from exc
        if affected != 1:
            raise DynamicResponseCacheUnavailable("dynamic response cache lease was lost")

    def fail(self, *, cache_key: str, lease_owner: str) -> None:
        try:
            db.execute(
                """
                UPDATE cache_dynamic_market_response
                SET state = 'failed', lease_owner = NULL, lease_expires_at = NULL, updated_at = %s
                WHERE cache_key = %s AND state = 'building' AND lease_owner = %s
                """,
                (datetime.now(), cache_key, lease_owner),
            )
        except MySQLError:
            logger.warning("dynamic_response_cache_fail_mark_failed", exc_info=True)
