"""Request-local memoization for read-only deep-analysis work."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, TypeVar


T = TypeVar("T")
_MISSING = object()
_CURRENT: ContextVar["RequestCache | None"] = ContextVar("deep_analysis_request_cache", default=None)


@dataclass(frozen=True, slots=True)
class RequestCacheStats:
    enabled: bool
    query_hits: int
    query_misses: int
    value_hits: int
    value_misses: int


@dataclass
class RequestCache:
    enabled: bool = True
    _queries: dict[tuple[str, tuple[Any, ...]], list[dict[str, Any]]] = field(default_factory=dict)
    _values: dict[tuple[str, tuple[Any, ...]], Any] = field(default_factory=dict)
    _query_hits: int = 0
    _query_misses: int = 0
    _value_hits: int = 0
    _value_misses: int = 0

    def query(self, sql: str, params: Sequence[Any] | None) -> tuple[bool, list[dict[str, Any]] | None]:
        if not self.enabled:
            return False, None
        key = (sql, tuple(_freeze(value) for value in (params or ())))
        rows = self._queries.get(key, _MISSING)
        if rows is _MISSING:
            self._query_misses += 1
            return False, None
        self._query_hits += 1
        return True, rows

    def store_query(self, sql: str, params: Sequence[Any] | None, rows: list[dict[str, Any]]) -> None:
        if self.enabled:
            key = (sql, tuple(_freeze(value) for value in (params or ())))
            self._queries[key] = rows

    def value(self, namespace: str, key: Sequence[Any]) -> tuple[bool, Any]:
        if not self.enabled:
            return False, None
        cache_key = (namespace, tuple(_freeze(value) for value in key))
        value = self._values.get(cache_key, _MISSING)
        if value is _MISSING:
            self._value_misses += 1
            return False, None
        self._value_hits += 1
        return True, value

    def store_value(self, namespace: str, key: Sequence[Any], value: Any) -> None:
        if self.enabled:
            cache_key = (namespace, tuple(_freeze(item) for item in key))
            self._values[cache_key] = value

    def get_or_set(self, namespace: str, key: Sequence[Any], loader: Callable[[], T]) -> T:
        found, value = self.value(namespace, key)
        if found:
            return value
        value = loader()
        self.store_value(namespace, key, value)
        return value

    def stats(self) -> RequestCacheStats:
        return RequestCacheStats(
            enabled=self.enabled,
            query_hits=self._query_hits,
            query_misses=self._query_misses,
            value_hits=self._value_hits,
            value_misses=self._value_misses,
        )


def current_request_cache() -> RequestCache | None:
    return _CURRENT.get()


@contextmanager
def request_cache_scope(*, enabled: bool = True) -> Iterator[RequestCache]:
    cache = RequestCache(enabled=enabled)
    token: Token[RequestCache | None] = _CURRENT.set(cache)
    try:
        yield cache
    finally:
        _CURRENT.reset(token)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_freeze(item) for item in value))
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value
