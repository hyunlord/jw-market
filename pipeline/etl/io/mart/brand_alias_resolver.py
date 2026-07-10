"""Central brand alias resolution without changing persisted canonical keys."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Iterable, Mapping
import unicodedata

from pipeline.etl.io.mart.brand_key_normalize import normalize_brand_name


MANUAL_BRAND_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {"위너프A+": "위너프에이플러스"}
)


class AliasReverseCollisionError(ValueError):
    """Raised when one alias points at more than one canonical key."""


class AliasShadowingError(ValueError):
    """Raised when an alias shadows a different canonical key."""


@dataclass(frozen=True, slots=True)
class BrandAliasResolver:
    aliases: Mapping[str, str]
    canonical_keys: frozenset[str]

    @classmethod
    def from_static(
        cls,
        aliases: Iterable[tuple[str, str]],
        *,
        canonical_keys: Iterable[str] = (),
    ) -> BrandAliasResolver:
        canonical = frozenset(_clean(value) for value in canonical_keys if _clean(value))
        resolved: dict[str, str] = {}
        for raw_alias, raw_brand_key in aliases:
            alias_name = _clean(raw_alias)
            brand_key = _clean(raw_brand_key)
            if not alias_name or not brand_key or alias_name == brand_key:
                continue
            previous = resolved.get(alias_name)
            if previous is not None and previous != brand_key:
                raise AliasReverseCollisionError(
                    f"alias maps to multiple brand keys: {alias_name}={previous},{brand_key}"
                )
            if alias_name in canonical and alias_name != brand_key:
                raise AliasShadowingError(
                    f"alias shadows another canonical key: {alias_name}->{brand_key}"
                )
            resolved[alias_name] = brand_key
        return cls(aliases=MappingProxyType(resolved), canonical_keys=canonical)

    @classmethod
    def from_connection(cls, conn: Any) -> BrandAliasResolver:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT brand_key
            FROM mart_general_brand_metric
            WHERE brand_key IS NOT NULL AND brand_key <> ''
            """
        )
        canonical_keys = tuple(_row_value(row, "brand_key", 0) for row in cursor.fetchall())
        cursor.execute("SELECT alias_name, brand_key FROM brand_alias")
        aliases = tuple(
            (_row_value(row, "alias_name", 0), _row_value(row, "brand_key", 1))
            for row in cursor.fetchall()
        )
        return cls.from_static(aliases, canonical_keys=canonical_keys)

    def resolve(self, name: Any) -> str:
        alias_name = _clean(name)
        canonical = normalize_brand_name(alias_name)
        if canonical in self.canonical_keys:
            return canonical
        return self.aliases.get(alias_name, canonical)

    def resolve_alias(self, name: Any) -> str:
        alias_name = _clean(name)
        return self.aliases.get(alias_name, alias_name)


def resolve_brand_key(name: Any, conn: Any) -> str:
    """Resolve a display/source name using current d2 canonical keys and aliases."""

    return BrandAliasResolver.from_connection(conn).resolve(name)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def _row_value(row: Any, key: str, index: int) -> str:
    if isinstance(row, Mapping):
        return str(row.get(key) or "")
    return str(row[index] or "")
