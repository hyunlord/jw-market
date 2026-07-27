from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from jw_chat_agent_poc.tools.general_view_backend import AtcCandidate


_BRAND_NORMALIZER = re.compile(r"[^0-9A-Za-zㄱ-힝]+")
_SHORTHAND_GRAM_SIZE = 4


class GeneralMembershipLoadError(RuntimeError):
    """Raised when the additive general membership snapshot cannot be loaded."""


@dataclass(frozen=True, slots=True)
class GeneralBrandMembership:
    brand_key: str
    brand_name: str
    atc4_code: str
    atc4_description: str
    source: str


@dataclass(frozen=True, slots=True)
class GeneralMembershipResolution:
    brand_key: str
    brand_name: str
    candidates: tuple[AtcCandidate, ...]


@dataclass(frozen=True, slots=True)
class _MembershipIndex:
    candidates: dict[str, dict[str, tuple[AtcCandidate, ...]]]
    aliases: dict[str, dict[str, tuple[str, ...]]]
    brand_keys: dict[str, dict[str, str]]
    brand_names: dict[str, dict[str, str]]
    terms: dict[str, dict[str, tuple[str, ...]]]
    grams: dict[str, dict[str, tuple[str, ...]]]


_EMPTY_INDEX = _MembershipIndex({}, {}, {}, {}, {}, {})


class GeneralMembershipReader(Protocol):
    def load(self) -> tuple[GeneralBrandMembership, ...]: ...


@dataclass(frozen=True, slots=True)
class StaticGeneralMembershipReader:
    memberships: tuple[GeneralBrandMembership, ...]

    def load(self) -> tuple[GeneralBrandMembership, ...]:
        return self.memberships


@dataclass(frozen=True, slots=True)
class MariaDbGeneralMembershipReader:
    host: str = os.environ.get("CHAT_CACHE_DB_HOST", "llmops-mariadb-service.llmops.svc.cluster.local")
    port: int = int(os.environ.get("CHAT_CACHE_DB_PORT", "3306"))
    database: str = os.environ.get("CHAT_CACHE_DB_NAME", "jw_mart")
    user: str = os.environ.get("CHAT_CACHE_DB_USER", "llmops")
    password: str = os.environ.get("CHAT_CACHE_DB_PASSWORD", "")
    table: str = os.environ.get("CHAT_GENERAL_MEMBERSHIP_TABLE", "chat_general_brand_membership")
    connect_timeout_s: int = int(os.environ.get("CHAT_CACHE_DB_CONNECT_TIMEOUT_S", "3"))
    read_timeout_s: int = int(os.environ.get("CHAT_CACHE_DB_READ_TIMEOUT_S", "5"))

    def load(self) -> tuple[GeneralBrandMembership, ...]:
        import pymysql

        table = _quote_identifier(self.table)
        try:
            with pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                connect_timeout=self.connect_timeout_s,
                read_timeout=self.read_timeout_s,
                write_timeout=self.read_timeout_s,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
            ) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        SELECT brand_key, brand_name, atc4_code, atc4_description, source
                        FROM {table}
                        ORDER BY normalized_brand_name, source, atc4_code
                        """
                    )
                    rows = cursor.fetchall()
        except pymysql.MySQLError as exc:
            raise GeneralMembershipLoadError("failed to load general membership snapshot") from exc

        return tuple(
            GeneralBrandMembership(
                brand_key=str(row["brand_key"]),
                brand_name=str(row["brand_name"]),
                atc4_code=str(row["atc4_code"]).upper(),
                atc4_description=str(row["atc4_description"] or f"ATC4 {row['atc4_code']}"),
                source=normalize_general_source(str(row["source"])),
            )
            for row in rows
        )


class TtlGeneralMembershipCache:
    """Replace-only membership snapshot with indexed, fail-closed shorthand lookup."""

    def __init__(self, reader: GeneralMembershipReader, *, ttl_seconds: float) -> None:
        self._reader = reader
        self._ttl_seconds = ttl_seconds
        self._expires_at = 0.0
        self._index = _EMPTY_INDEX
        self._lock = threading.Lock()
        self._row_count = 0
        self._loaded_at = 0.0
        self._refresh_successes = 0
        self._refresh_failures = 0

    def candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]:
        resolution = self.resolve(brand, source)
        return resolution.candidates if resolution is not None else ()

    def resolve(self, brand: str, source: str) -> GeneralMembershipResolution | None:
        self._refresh_if_expired()
        normalized = normalize_general_brand(brand)
        normalized_source = normalize_general_source(source)
        source_candidates = self._index.candidates.get(normalized_source, {})
        canonical = normalized if normalized in source_candidates else None
        if canonical is None:
            exact_aliases = self._index.aliases.get(normalized_source, {}).get(normalized, ())
            if len(exact_aliases) == 1:
                canonical = exact_aliases[0]
        if canonical is None and len(normalized) >= _SHORTHAND_GRAM_SIZE:
            gram = normalized[:_SHORTHAND_GRAM_SIZE]
            possible = self._index.grams.get(normalized_source, {}).get(gram, ())
            matches = {
                candidate
                for candidate in possible
                if any(
                    normalized in term
                    for term in self._index.terms.get(normalized_source, {}).get(candidate, ())
                )
            }
            if len(matches) == 1:
                canonical = next(iter(matches))
        if canonical is None:
            return None
        candidates = source_candidates.get(canonical, ())
        if not candidates:
            return None
        return GeneralMembershipResolution(
            brand_key=self._index.brand_keys[normalized_source][canonical],
            brand_name=self._index.brand_names[normalized_source][canonical],
            candidates=candidates,
        )

    def _refresh_if_expired(self) -> None:
        now = time.monotonic()
        if now < self._expires_at:
            return
        with self._lock:
            now = time.monotonic()
            if now < self._expires_at:
                return
            try:
                memberships = self._reader.load()
            except Exception:
                self._refresh_failures += 1
                raise
            self._index = _build_index(memberships)
            self._row_count = len(memberships)
            self._loaded_at = now
            self._refresh_successes += 1
            self._expires_at = now + self._ttl_seconds

    def observability(self) -> dict[str, int | float | None]:
        with self._lock:
            return {
                "row_count": self._row_count,
                "snapshot_age_seconds": (
                    round(max(0.0, time.monotonic() - self._loaded_at), 3)
                    if self._loaded_at
                    else None
                ),
                "refresh_successes": self._refresh_successes,
                "refresh_failures": self._refresh_failures,
            }


def _build_index(memberships: tuple[GeneralBrandMembership, ...]) -> _MembershipIndex:
    candidates_by_source: dict[str, dict[str, dict[str, AtcCandidate]]] = {}
    alias_sets: dict[str, dict[str, set[str]]] = {}
    brand_keys: dict[str, dict[str, str]] = {}
    brand_names: dict[str, dict[str, str]] = {}
    term_sets: dict[str, dict[str, set[str]]] = {}
    for membership in memberships:
        source = normalize_general_source(membership.source)
        canonical = normalize_general_brand(membership.brand_key)
        if not canonical:
            continue
        candidate = AtcCandidate(membership.atc4_code.upper(), membership.atc4_description)
        source_candidates = candidates_by_source.setdefault(source, {})
        source_candidates.setdefault(canonical, {})[candidate.code] = candidate
        brand_keys.setdefault(source, {}).setdefault(canonical, membership.brand_key)
        brand_names.setdefault(source, {}).setdefault(canonical, membership.brand_name)
        terms = term_sets.setdefault(source, {}).setdefault(canonical, set())
        terms.add(canonical)
        normalized_name = normalize_general_brand(membership.brand_name)
        if normalized_name:
            terms.add(normalized_name)
            if normalized_name != canonical:
                alias_sets.setdefault(source, {}).setdefault(normalized_name, set()).add(canonical)
    candidates = {
        source: {
            canonical: tuple(candidates[code] for code in sorted(candidates))
            for canonical, candidates in source_candidates.items()
        }
        for source, source_candidates in candidates_by_source.items()
    }
    terms = {
        source: {canonical: tuple(sorted(values)) for canonical, values in by_canonical.items()}
        for source, by_canonical in term_sets.items()
    }
    gram_sets: dict[str, dict[str, set[str]]] = {}
    for source, by_canonical in terms.items():
        for canonical, values in by_canonical.items():
            for value in values:
                for offset in range(len(value) - _SHORTHAND_GRAM_SIZE + 1):
                    gram = value[offset : offset + _SHORTHAND_GRAM_SIZE]
                    gram_sets.setdefault(source, {}).setdefault(gram, set()).add(canonical)
    return _MembershipIndex(
        candidates=candidates,
        aliases={
            source: {alias: tuple(sorted(values)) for alias, values in by_alias.items()}
            for source, by_alias in alias_sets.items()
        },
        brand_keys=brand_keys,
        brand_names=brand_names,
        terms=terms,
        grams={
            source: {gram: tuple(sorted(values)) for gram, values in by_gram.items()}
            for source, by_gram in gram_sets.items()
        },
    )


def normalize_general_brand(value: str) -> str:
    return _BRAND_NORMALIZER.sub("", value).lower()


def normalize_general_source(value: str) -> str:
    normalized = value.strip().lower()
    return "iqvia" if normalized in {"iqvia", "iqvia_nsa", "nsa"} else normalized


def _quote_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError("invalid membership table identifier")
    return f"`{value}`"
