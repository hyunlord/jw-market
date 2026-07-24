"""Versioned Agent2 event-brand mappings and intentional exclusions."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Any, Final, Iterable, Literal, Mapping
import unicodedata

from pipeline.etl.io.mart.brand_alias_resolver import MANUAL_BRAND_ALIASES


RegistryAction = Literal["map", "exclude"]


@dataclass(frozen=True, slots=True)
class Agent2BrandRegistryEntry:
    source_name: str
    action: RegistryAction
    canonical_name: str | None
    owner: str
    reason: str


AGENT2_BRAND_REGISTRY_ENTRIES: Final = (
    Agent2BrandRegistryEntry(
        source_name="리조덱",
        action="map",
        canonical_name="리조덱플렉스터치",
        owner="PL",
        reason="single unambiguous mart and strategic brand candidate",
    ),
    Agent2BrandRegistryEntry(
        source_name="트레시바",
        action="map",
        canonical_name="트레시바플렉스터치",
        owner="PL",
        reason="single unambiguous mart and strategic brand candidate",
    ),
    Agent2BrandRegistryEntry(
        source_name="염화칼륨",
        action="exclude",
        canonical_name=None,
        owner="PL",
        reason="ingredient label has five brand candidates",
    ),
    Agent2BrandRegistryEntry(
        source_name="하트만",
        action="exclude",
        canonical_name=None,
        owner="PL",
        reason="solution-family label has seven brand candidates",
    ),
    Agent2BrandRegistryEntry(
        source_name="오메가",
        action="exclude",
        canonical_name=None,
        owner="PL",
        reason="class label has seventeen brand candidates",
    ),
)


def _clean(value: object) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def _registry_revision() -> str:
    payload = {
        "schema": "agent2-brand-registry/v1",
        "manual_aliases": dict(sorted(MANUAL_BRAND_ALIASES.items())),
        "entries": [
            {
                "source_name": entry.source_name,
                "action": entry.action,
                "canonical_name": entry.canonical_name,
                "owner": entry.owner,
                "reason": entry.reason,
            }
            for entry in AGENT2_BRAND_REGISTRY_ENTRIES
        ],
        "unknown_alias": "hard_fail",
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


AGENT2_BRAND_REGISTRY_REVISION: Final = _registry_revision()


class UnknownAgent2BrandAliasError(RuntimeError):
    def __init__(self, source_name: str) -> None:
        super().__init__(f"unregistered Agent2 event brand alias: {source_name}")
        self.source_name = source_name


class InvalidAgent2BrandRegistryError(RuntimeError):
    """Raised when a mapping target is absent from the current brand universe."""


@dataclass(frozen=True, slots=True)
class Agent2BrandRegistry:
    canonical_names: frozenset[str]
    mappings: Mapping[str, str]
    exclusions: frozenset[str]
    revision: str = AGENT2_BRAND_REGISTRY_REVISION

    @classmethod
    def for_canonical_names(
        cls,
        canonical_names: Iterable[str],
        *,
        strict_targets: bool = False,
    ) -> Agent2BrandRegistry:
        canonical = frozenset(
            cleaned for value in canonical_names if (cleaned := _clean(value))
        )
        mappings = {
            _clean(alias): _clean(target)
            for alias, target in MANUAL_BRAND_ALIASES.items()
        }
        exclusions: set[str] = set()
        for entry in AGENT2_BRAND_REGISTRY_ENTRIES:
            if entry.action == "map":
                target = _clean(entry.canonical_name)
                if strict_targets and target not in canonical:
                    raise InvalidAgent2BrandRegistryError(
                        f"Agent2 alias target is absent from canonical names: "
                        f"{entry.source_name}->{target}"
                    )
                if target in canonical:
                    mappings[_clean(entry.source_name)] = target
            else:
                exclusions.add(_clean(entry.source_name))
        return cls(
            canonical_names=canonical,
            mappings=MappingProxyType(mappings),
            exclusions=frozenset(exclusions),
        )

    def resolve(self, source_name: object) -> str | None:
        source = _clean(source_name)
        if not source:
            raise UnknownAgent2BrandAliasError(source)
        if source in self.exclusions:
            return None
        mapped = self.mappings.get(source)
        if mapped is not None and mapped in self.canonical_names:
            return mapped
        if source in self.canonical_names:
            return source
        raise UnknownAgent2BrandAliasError(source)

    def source_names_for(self, canonical_name: object) -> tuple[str, ...]:
        canonical = _clean(canonical_name)
        if canonical not in self.canonical_names:
            raise UnknownAgent2BrandAliasError(canonical)
        aliases = {
            alias
            for alias, target in self.mappings.items()
            if target == canonical
        }
        return tuple(sorted({canonical, *aliases}))


def event_brand_source_names(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the centrally owned event-to-brand source-name candidates."""

    names = {
        cleaned
        for field in ("brand_canonical", "brand_name")
        if (cleaned := _clean(row.get(field)))
    }
    if _clean(row.get("derivation")) == "cross_match":
        raw_mirrors = row.get("mirrored_from_jw_brands")
        try:
            mirrors = json.loads(raw_mirrors or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            mirrors = []
        if isinstance(mirrors, list):
            names.update(
                cleaned
                for value in mirrors
                if (cleaned := _clean(value))
            )
    return tuple(sorted(names))


def event_brand_match_sql(
    source_names: Iterable[str],
    *,
    score_alias: str = "s",
) -> tuple[str, tuple[str, ...]]:
    """Build the SQL equivalent of :func:`event_brand_source_names`."""

    names = tuple(
        sorted({cleaned for value in source_names if (cleaned := _clean(value))})
    )
    if not names:
        return "1 = 0", ()
    marks = ",".join("%s" for _ in names)
    mirror_clauses = " OR ".join(
        f"{score_alias}.mirrored_from_jw_brands LIKE %s" for _ in names
    )
    predicate = (
        f"{score_alias}.brand_canonical IN ({marks}) "
        f"OR {score_alias}.brand_name IN ({marks}) "
        f"OR ({score_alias}.derivation = 'cross_match' AND ({mirror_clauses}))"
    )
    return (
        predicate,
        (
            *names,
            *names,
            *(f'%"{name}"%' for name in names),
        ),
    )
