from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import monotonic
from typing import Any, Protocol, Self

import requests

from jw_chat_agent_poc.tools.external.hira_reimbursement_parser import (
    detail_text,
    is_official_hira_url,
    matching_search_row,
)

HIRA_REIMBURSEMENT_LIST_URL = (
    "https://www.hira.or.kr/rc/insu/insuadtcrtr/InsuAdtCrtrList.do"
)
HIRA_REIMBURSEMENT_PGMID = "HIRAA030069000410"
HIRA_REALTIME_TIMEOUT_S = 6.0
REIMBURSEMENT_FRESHNESS = timedelta(days=2)
_MAX_BRAND_LENGTH = 80
LOGGER = logging.getLogger(__name__)


class CacheStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    NOT_FOUND = "NOT_FOUND"


class ReimbursementRefreshError(RuntimeError):
    """The background refresh dispatcher could not accept a refresh request."""


class ReimbursementStoreError(RuntimeError):
    """The confirmed crawler store could not persist a verified criterion."""


@dataclass(frozen=True, slots=True)
class InvalidReimbursementBrand(ValueError):
    brand_name: str

    def __str__(self) -> str:
        return "brand_name must contain 1-80 non-blank characters"


@dataclass(frozen=True, slots=True)
class ReimbursementCriterion:
    brand_name: str
    title: str
    raw_text: str
    source_date: str | None
    collected_at: datetime
    notice_number: str | None
    source_url: str


@dataclass(frozen=True, slots=True)
class ReimbursementCacheResult:
    status: CacheStatus
    data: ReimbursementCriterion | None
    source_date: str | None


@dataclass(frozen=True, slots=True)
class ReimbursementLookupResult:
    ok: bool
    cache_status: CacheStatus
    retrieval: str
    data: ReimbursementCriterion | None
    error_code: str | None = None
    cache_write: str = "not_attempted"


class ReimbursementCriteriaStore(Protocol):
    def get_reimbursement_criteria(self, brand_name: str) -> ReimbursementCacheResult: ...

    def put_reimbursement_criteria(self, criterion: ReimbursementCriterion) -> bool: ...


class ReimbursementRealtimeClient(Protocol):
    def fetch(self, brand_name: str) -> ReimbursementCriterion | None: ...


class ReimbursementRefreshTrigger(Protocol):
    def __call__(self, brand_name: str) -> None: ...


class _Cursor(Protocol):
    def execute(self, sql: str, params: object = None) -> Any: ...

    def fetchone(self) -> dict[str, Any] | None: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, *args: object) -> None: ...


class _Connection(Protocol):
    def cursor(self) -> _Cursor: ...

    def close(self) -> None: ...


class AbsentReimbursementStore:
    """Explicit adapter for the pre-crawler state; it never pretends persistence exists."""

    def get_reimbursement_criteria(self, _brand_name: str) -> ReimbursementCacheResult:
        return ReimbursementCacheResult(CacheStatus.NOT_FOUND, None, None)

    def put_reimbursement_criteria(self, _criterion: ReimbursementCriterion) -> bool:
        return False


class MariaDbReimbursementStore:
    """Read the agent-owned HIRA cache without taking ownership of its writes."""

    def __init__(self, *, connect: Callable[[], _Connection] | None = None) -> None:
        self._connect = connect or _connect_reimbursement_db

    def get_reimbursement_criteria(self, brand_name: str) -> ReimbursementCacheResult:
        connection: _Connection | None = None
        try:
            connection = self._connect()
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                      b.brand_name,
                      n.title,
                      n.raw_text,
                      n.notice_date,
                      n.collected_at,
                      n.notice_no,
                      n.source_url
                    FROM hira_benefit_notice_brand AS b
                    INNER JOIN hira_benefit_notice AS n
                      ON n.source_notice_id = b.source_notice_id
                    WHERE b.brand_name = %s
                      AND NULLIF(TRIM(n.raw_text), '') IS NOT NULL
                    ORDER BY
                      n.notice_date DESC,
                      n.collected_at DESC,
                      n.source_notice_id DESC
                    LIMIT 1
                    """,
                    (brand_name,),
                )
                row = cursor.fetchone()
        except Exception as exc:
            raise ReimbursementStoreError(
                f"reimbursement cache read failed: {type(exc).__name__}"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

        if row is None:
            return ReimbursementCacheResult(CacheStatus.NOT_FOUND, None, None)
        criterion = _criterion_from_cache_row(row)
        if not criterion.raw_text.strip():
            return ReimbursementCacheResult(CacheStatus.NOT_FOUND, None, None)
        return ReimbursementCacheResult(
            CacheStatus.FRESH,
            criterion,
            criterion.source_date,
        )

    def put_reimbursement_criteria(self, _criterion: ReimbursementCriterion) -> bool:
        return False


def configured_reimbursement_store() -> ReimbursementCriteriaStore:
    required = (
        os.environ.get("CHAT_CACHE_DB_HOST", "").strip(),
        os.environ.get("CHAT_CACHE_DB_NAME", "").strip(),
        os.environ.get("CHAT_CACHE_DB_USER", "").strip(),
        os.environ.get("CHAT_CACHE_DB_PASSWORD", ""),
    )
    if not all(required):
        return AbsentReimbursementStore()
    return MariaDbReimbursementStore()


class ReimbursementLookupService:
    def __init__(
        self,
        *,
        store: ReimbursementCriteriaStore,
        realtime: ReimbursementRealtimeClient,
        refresh_trigger: ReimbursementRefreshTrigger | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._realtime = realtime
        self._refresh_trigger = refresh_trigger or (lambda _brand: None)
        self._now = now or (lambda: datetime.now(UTC))

    def lookup(self, brand_name: str) -> ReimbursementLookupResult:
        brand = _validated_brand(brand_name)
        try:
            cached = self._store.get_reimbursement_criteria(brand)
        except ReimbursementStoreError as exc:
            LOGGER.warning(
                "reimbursement cache read failed error=%s",
                type(exc).__name__,
            )
            cached = ReimbursementCacheResult(CacheStatus.NOT_FOUND, None, None)
        status = _effective_cache_status(cached, now=self._now())

        if status is CacheStatus.FRESH and cached.data is not None:
            return ReimbursementLookupResult(True, status, "cache", cached.data)
        if status is CacheStatus.STALE and cached.data is not None:
            try:
                self._refresh_trigger(brand)
            except ReimbursementRefreshError as exc:
                LOGGER.warning(
                    "reimbursement refresh trigger failed error=%s",
                    type(exc).__name__,
                )
            return ReimbursementLookupResult(True, status, "stale_cache", cached.data)

        try:
            live = self._realtime.fetch(brand)
        except requests.Timeout:
            return ReimbursementLookupResult(
                False,
                CacheStatus.NOT_FOUND,
                "typed_unavailable",
                None,
                error_code="TOOL_TIMEOUT",
            )
        except requests.RequestException:
            return ReimbursementLookupResult(
                False,
                CacheStatus.NOT_FOUND,
                "typed_unavailable",
                None,
                error_code="UPSTREAM_UNAVAILABLE",
            )

        if live is None:
            return ReimbursementLookupResult(
                False,
                CacheStatus.NOT_FOUND,
                "typed_unavailable",
                None,
                error_code="NO_EVIDENCE",
            )
        try:
            persisted = self._store.put_reimbursement_criteria(live)
            cache_write = "stored" if persisted else "skipped_store_unavailable"
        except ReimbursementStoreError as exc:
            LOGGER.warning(
                "reimbursement cache write failed error=%s",
                type(exc).__name__,
            )
            cache_write = "failed"
        return ReimbursementLookupResult(
            True,
            CacheStatus.NOT_FOUND,
            "realtime",
            live,
            cache_write=cache_write,
        )


class HiraReimbursementHttpClient:
    """Bounded point lookup for the official HIRA insurance criteria page."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout_s: float = HIRA_REALTIME_TIMEOUT_S,
        now: Callable[[], datetime] | None = None,
        monotonic_now: Callable[[], float] | None = None,
    ) -> None:
        if not 0 < timeout_s <= 8:
            raise ValueError("timeout_s must be within the approved 0-8 second boundary")
        self._session = session or requests.Session()
        self._timeout_s = timeout_s
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic_now or monotonic

    def fetch(self, brand_name: str) -> ReimbursementCriterion | None:
        brand = _validated_brand(brand_name)
        deadline = self._monotonic() + self._timeout_s
        response = self._session.get(
            HIRA_REIMBURSEMENT_LIST_URL,
            params={
                "pgmid": HIRA_REIMBURSEMENT_PGMID,
                "searchCondition": "TXTALL",
                "searchWord": brand,
                "pageIndex": 1,
                "pageSize": 10,
            },
            timeout=self._request_timeout(deadline),
            allow_redirects=False,
        )
        response.raise_for_status()
        if _is_redirect(response.status_code):
            return None
        match = matching_search_row(response.text, brand, response.url)
        if match is None or not is_official_hira_url(match.url):
            return None

        detail = self._session.get(
            match.url,
            timeout=self._request_timeout(deadline),
            allow_redirects=False,
        )
        detail.raise_for_status()
        if _is_redirect(detail.status_code) or not is_official_hira_url(detail.url):
            return None
        raw_text = detail_text(detail.text)
        if not raw_text:
            return None
        return ReimbursementCriterion(
            brand_name=brand,
            title=match.title,
            raw_text=raw_text,
            source_date=match.source_date,
            collected_at=self._now(),
            notice_number=match.notice_number,
            source_url=detail.url,
        )

    def _request_timeout(self, deadline: float) -> tuple[float, float]:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise requests.Timeout("HIRA reimbursement lookup deadline exceeded")
        connect = max(min(3.0, remaining / 3), 0.001)
        read = max(remaining - connect, 0.001)
        return connect, read

def _effective_cache_status(
    cached: ReimbursementCacheResult,
    *,
    now: datetime,
) -> CacheStatus:
    if cached.data is None or not cached.data.raw_text.strip():
        return CacheStatus.NOT_FOUND
    collected_at = cached.data.collected_at
    if collected_at.tzinfo is None:
        collected_at = collected_at.replace(tzinfo=UTC)
    age = now - collected_at
    return (
        CacheStatus.FRESH
        if timedelta(0) <= age <= REIMBURSEMENT_FRESHNESS
        else CacheStatus.STALE
    )


def _connect_reimbursement_db() -> _Connection:
    import pymysql

    return pymysql.connect(
        host=os.environ["CHAT_CACHE_DB_HOST"],
        port=int(os.environ.get("CHAT_CACHE_DB_PORT", "3306")),
        user=os.environ["CHAT_CACHE_DB_USER"],
        password=os.environ["CHAT_CACHE_DB_PASSWORD"],
        database=os.environ["CHAT_CACHE_DB_NAME"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=2,
        read_timeout=2,
        write_timeout=2,
        autocommit=True,
    )


def _criterion_from_cache_row(row: dict[str, Any]) -> ReimbursementCriterion:
    collected_at = row.get("collected_at")
    if isinstance(collected_at, str):
        collected_at = datetime.fromisoformat(collected_at)
    if not isinstance(collected_at, datetime):
        raise ReimbursementStoreError("reimbursement cache collected_at is invalid")
    if collected_at.tzinfo is None:
        collected_at = collected_at.replace(tzinfo=UTC)
    source_date = row.get("notice_date")
    return ReimbursementCriterion(
        brand_name=str(row.get("brand_name") or ""),
        title=str(row.get("title") or "HIRA 보험인정기준"),
        raw_text=str(row.get("raw_text") or ""),
        source_date=None if source_date is None else str(source_date),
        collected_at=collected_at,
        notice_number=None if row.get("notice_no") is None else str(row["notice_no"]),
        source_url=str(row.get("source_url") or ""),
    )


def _validated_brand(brand_name: str) -> str:
    brand = re.sub(r"\s+", " ", str(brand_name).strip())
    if not brand or len(brand) > _MAX_BRAND_LENGTH:
        raise InvalidReimbursementBrand(brand_name)
    return brand


def _is_redirect(status_code: int) -> bool:
    return 300 <= status_code < 400
