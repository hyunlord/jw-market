from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jw_chat_agent_poc.tools.external.client import ExternalCall


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    expires_at: float
    value: ExternalCall


class ExternalResultCache:
    def __init__(
        self,
        *,
        ttl_seconds: int,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = max(0, ttl_seconds)
        self._max_entries = max(1, max_entries)
        self._clock = clock
        self._entries: dict[tuple[Hashable, ...], _CacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple[Hashable, ...]) -> ExternalCall | None:
        if self._ttl_seconds == 0:
            return None
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._entries.pop(key, None)
                return None
            return entry.value

    def put(self, key: tuple[Hashable, ...], value: ExternalCall) -> None:
        if self._ttl_seconds == 0 or not _has_cacheable_evidence(value):
            return
        with self._lock:
            if len(self._entries) >= self._max_entries and key not in self._entries:
                oldest = min(self._entries, key=lambda candidate: self._entries[candidate].expires_at)
                self._entries.pop(oldest, None)
            self._entries[key] = _CacheEntry(self._clock() + self._ttl_seconds, value)


def _has_cacheable_evidence(call: ExternalCall) -> bool:
    if call.status != "live":
        return False
    data = call.render_data
    if isinstance(data.get("items"), list) and data["items"]:
        return True
    payload = data.get("payload")
    if isinstance(payload, list):
        return bool(payload)
    if isinstance(payload, dict):
        for key in ("studies", "results", "items"):
            if isinstance(payload.get(key), list) and payload[key]:
                return True
    return False


_SHARED_CACHES: dict[tuple[int, int], ExternalResultCache] = {}
_SHARED_CACHES_LOCK = threading.Lock()


def shared_external_result_cache(*, ttl_seconds: int, max_entries: int) -> ExternalResultCache:
    key = (ttl_seconds, max_entries)
    with _SHARED_CACHES_LOCK:
        cache = _SHARED_CACHES.get(key)
        if cache is None:
            cache = ExternalResultCache(ttl_seconds=ttl_seconds, max_entries=max_entries)
            _SHARED_CACHES[key] = cache
        return cache
