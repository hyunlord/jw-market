from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import logging
import re
from time import monotonic
from typing import Callable, Protocol

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
    criterion_text: str
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


class AbsentReimbursementStore:
    """Explicit adapter for the pre-crawler state; it never pretends persistence exists."""

    def get_reimbursement_criteria(self, _brand_name: str) -> ReimbursementCacheResult:
        return ReimbursementCacheResult(CacheStatus.NOT_FOUND, None, None)

    def put_reimbursement_criteria(self, _criterion: ReimbursementCriterion) -> bool:
        return False


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
        criterion_text = detail_text(detail.text)
        if not criterion_text:
            return None
        return ReimbursementCriterion(
            brand_name=brand,
            title=match.title,
            criterion_text=criterion_text,
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
    if cached.data is None:
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

def _validated_brand(brand_name: str) -> str:
    brand = re.sub(r"\s+", " ", str(brand_name).strip())
    if not brand or len(brand) > _MAX_BRAND_LENGTH:
        raise InvalidReimbursementBrand(brand_name)
    return brand


def _is_redirect(status_code: int) -> bool:
    return 300 <= status_code < 400
