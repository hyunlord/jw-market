from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
import logging
from types import MappingProxyType
from typing import Any, Final


logger = logging.getLogger(__name__)


class _Missing:
    __slots__ = ()


_MISSING: Final = _Missing()
_POINT_KEYS: Final[tuple[str, ...]] = (
    "raw_value",
    "ms",
    "rank",
    "growth_abs",
    "mat",
    "mom",
    "qoq",
    "yoy",
    "source_status",
    "status",
    "brand",
)
_POINT_KEY_SET: Final[frozenset[str]] = frozenset(_POINT_KEYS)


@dataclass(frozen=True, slots=True)
class RawValuePoint(Mapping[str, Any]):
    """Compact representation for the dominant one-key mart JSON point."""

    raw_value: Any

    def __getitem__(self, key: str) -> Any:
        if key == "raw_value":
            return self.raw_value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        yield "raw_value"

    def __len__(self) -> int:
        return 1


@dataclass(frozen=True, slots=True)
class MartJsonPoint(Mapping[str, Any]):
    """Mapping-compatible point preserving absent keys and future additions."""

    raw_value: Any = _MISSING
    ms: Any = _MISSING
    rank: Any = _MISSING
    growth_abs: Any = _MISSING
    mat: Any = _MISSING
    mom: Any = _MISSING
    qoq: Any = _MISSING
    yoy: Any = _MISSING
    source_status: Any = _MISSING
    status: Any = _MISSING
    brand: Any = _MISSING
    extra: Mapping[str, Any] | None = field(default=None, repr=False)

    def __getitem__(self, key: str) -> Any:
        if key in _POINT_KEY_SET:
            value = getattr(self, key)
            if value is not _MISSING:
                return value
        elif self.extra is not None and key in self.extra:
            return self.extra[key]
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        for key in _POINT_KEYS:
            if getattr(self, key) is not _MISSING:
                yield key
        if self.extra is not None:
            yield from self.extra

    def __len__(self) -> int:
        known = sum(getattr(self, key) is not _MISSING for key in _POINT_KEYS)
        return known + (len(self.extra) if self.extra is not None else 0)

    @property
    def unknown_key_count(self) -> int:
        return len(self.extra) if self.extra is not None else 0


def compact_mart_json(value: dict[str, Any], *, column: str) -> dict[str, Any]:
    """Replace value-point dicts recursively while preserving container dicts."""

    return {key: _compact_node(item, column=column) for key, item in value.items()}


def _compact_node(value: Any, *, column: str) -> Any:
    if not isinstance(value, dict):
        return value
    if "raw_value" in value:
        return _point(value, column=column)
    return {key: _compact_node(item, column=column) for key, item in value.items()}


def _point(value: dict[str, Any], *, column: str) -> Mapping[str, Any]:
    if value.keys() == {"raw_value"}:
        return RawValuePoint(value["raw_value"])

    known = {key: value[key] for key in _POINT_KEYS if key in value}
    unknown = {key: item for key, item in value.items() if key not in _POINT_KEY_SET}
    if unknown:
        _warn_unknown_keys(column, tuple(sorted(unknown)))
    return MartJsonPoint(
        **known,
        extra=MappingProxyType(unknown) if unknown else None,
    )


@lru_cache(maxsize=128)
def _warn_unknown_keys(column: str, keys: tuple[str, ...]) -> None:
    logger.warning(
        "unknown mart JSON point keys preserved: column=%s count=%d keys=%s",
        column,
        len(keys),
        ",".join(keys),
    )
