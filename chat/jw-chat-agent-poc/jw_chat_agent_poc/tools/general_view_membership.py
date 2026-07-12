from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from jw_chat_agent_poc.tools.general_view_backend import AtcCandidate


_BRAND_NORMALIZER = re.compile(r"[^0-9A-Za-zㄱ-힝]+")


class GeneralMembershipLoadError(RuntimeError):
    """Raised when the additive general membership snapshot cannot be loaded."""


@dataclass(frozen=True, slots=True)
class GeneralBrandMembership:
    brand_key: str
    brand_name: str
    atc4_code: str
    atc4_description: str
    source: str


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
                source=str(row["source"]).lower(),
            )
            for row in rows
        )


class TtlGeneralMembershipCache:
    """Replace-only exact membership snapshot; mutation is confined to TTL refresh."""

    def __init__(self, reader: GeneralMembershipReader, *, ttl_seconds: float) -> None:
        self._reader = reader
        self._ttl_seconds = ttl_seconds
        self._expires_at = 0.0
        self._snapshot: dict[str, dict[str, tuple[AtcCandidate, ...]]] = {}
        self._aliases: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    def candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]:
        self._refresh_if_expired()
        normalized = normalize_general_brand(brand)
        normalized_source = source.strip().lower()
        source_aliases = self._aliases.get(normalized_source, {})
        canonical = source_aliases.get(normalized, normalized)
        return self._snapshot.get(normalized_source, {}).get(canonical, ())

    def _refresh_if_expired(self) -> None:
        now = time.monotonic()
        if now < self._expires_at:
            return
        with self._lock:
            now = time.monotonic()
            if now < self._expires_at:
                return
            self._snapshot, self._aliases = _build_snapshot(self._reader.load())
            self._expires_at = now + self._ttl_seconds


def _build_snapshot(
    memberships: tuple[GeneralBrandMembership, ...],
) -> tuple[dict[str, dict[str, tuple[AtcCandidate, ...]]], dict[str, dict[str, str]]]:
    candidates_by_source: dict[str, dict[str, dict[str, AtcCandidate]]] = {}
    aliases: dict[str, dict[str, str]] = {}
    for membership in memberships:
        source = membership.source.strip().lower()
        canonical = normalize_general_brand(membership.brand_key)
        if not canonical:
            continue
        candidate = AtcCandidate(membership.atc4_code.upper(), membership.atc4_description)
        source_candidates = candidates_by_source.setdefault(source, {})
        source_candidates.setdefault(canonical, {})[candidate.code] = candidate
        normalized_name = normalize_general_brand(membership.brand_name)
        if normalized_name and normalized_name != canonical:
            aliases.setdefault(source, {})[normalized_name] = canonical
    snapshot = {
        source: {
            canonical: tuple(candidates[code] for code in sorted(candidates))
            for canonical, candidates in source_candidates.items()
        }
        for source, source_candidates in candidates_by_source.items()
    }
    return snapshot, aliases


def normalize_general_brand(value: str) -> str:
    return _BRAND_NORMALIZER.sub("", value).lower()


def _quote_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError("invalid membership table identifier")
    return f"`{value}`"
