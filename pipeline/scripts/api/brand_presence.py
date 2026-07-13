from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from threading import RLock
from time import monotonic
from typing import Final

from pipeline.scripts.api import db
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.types import quote_identifier


MISSING_BRAND_TTL_SECONDS: Final[float] = 60.0
MISSING_BRAND_MAX_ENTRIES: Final[int] = 1_024


def brand_exists(brand: str | None) -> bool:
    normalized = (brand or "").strip()
    if not normalized:
        return False

    table = f"{quote_identifier(config.db_name)}.mart_general_brand_metric"
    if db.fetch_one(
        f"SELECT 1 AS found FROM {table} WHERE brand_key = %s LIMIT 1",
        (normalized,),
    ):
        return True
    return bool(
        db.fetch_one(
            f"SELECT 1 AS found FROM {table} WHERE brand_name = %s LIMIT 1",
            (normalized,),
        )
    )


class NegativeBrandCache:
    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_entries: int,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._lock = RLock()

    def contains(self, brand: str | None) -> bool:
        normalized = (brand or "").strip()
        if not normalized:
            return False
        now = self._clock()
        with self._lock:
            expires_at = self._entries.get(normalized)
            if expires_at is None:
                return False
            if expires_at <= now:
                self._entries.pop(normalized, None)
                return False
            self._entries.move_to_end(normalized)
            return True

    def remember(self, brand: str | None) -> None:
        normalized = (brand or "").strip()
        if not normalized:
            return
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            self._entries[normalized] = now + self._ttl_seconds
            self._entries.move_to_end(normalized)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def discard(self, brand: str | None) -> None:
        normalized = (brand or "").strip()
        if not normalized:
            return
        with self._lock:
            self._entries.pop(normalized, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _purge_expired(self, now: float) -> None:
        expired = [brand for brand, expires_at in self._entries.items() if expires_at <= now]
        for brand in expired:
            self._entries.pop(brand, None)


missing_brand_cache = NegativeBrandCache(
    ttl_seconds=MISSING_BRAND_TTL_SECONDS,
    max_entries=MISSING_BRAND_MAX_ENTRIES,
)
