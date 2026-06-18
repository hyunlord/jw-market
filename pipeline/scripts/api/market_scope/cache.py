"""Local scope-hash cache used by Stage 0-2 tests and future endpoints."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ScopeHashCache:
    """Small in-memory cache keyed by canonical ``scope_hash``."""

    _items: dict[str, dict[str, Any]] = field(default_factory=dict)

    def read(self, scope_hash: str) -> dict[str, Any] | None:
        """Return a cached payload for ``scope_hash`` if present."""

        value = self._items.get(scope_hash)
        return deepcopy(value) if value is not None else None

    def write(self, scope_hash: str, payload: dict[str, Any]) -> None:
        """Store an isolated payload copy under ``scope_hash``."""

        self._items[scope_hash] = deepcopy(payload)
