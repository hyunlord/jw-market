from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
import threading
import time
from typing import Protocol


logger = logging.getLogger(__name__)


class BrandMoleculeSource(Protocol):
    def load(self) -> tuple[dict[str, str], ...]: ...


@dataclass(frozen=True, slots=True)
class StaticBrandMoleculeReader:
    rows: tuple[dict[str, str], ...]

    def brand_molecules(self) -> tuple[dict[str, str], ...]:
        return self.rows


@dataclass(frozen=True, slots=True)
class MariaDbBrandMoleculeSource:
    host: str = field(
        default_factory=lambda: os.environ.get(
            "CHAT_MOLECULE_DB_HOST",
            os.environ.get(
                "CHAT_QUERY_DB_HOST",
                os.environ.get("CHAT_CATALOG_DB_HOST", "llmops-mariadb-service.llmops.svc.cluster.local"),
            ),
        )
    )
    port: int = field(
        default_factory=lambda: int(
            os.environ.get(
                "CHAT_MOLECULE_DB_PORT",
                os.environ.get("CHAT_QUERY_DB_PORT", os.environ.get("CHAT_CATALOG_DB_PORT", "3306")),
            )
        )
    )
    database: str = field(
        default_factory=lambda: os.environ.get(
            "CHAT_MOLECULE_DB_NAME",
            os.environ.get("CHAT_QUERY_DB_NAME", os.environ.get("CHAT_CATALOG_DB_NAME", "jw_mart")),
        )
    )
    user: str = field(
        default_factory=lambda: os.environ.get(
            "CHAT_MOLECULE_DB_USER",
            os.environ.get("CHAT_QUERY_DB_USER", os.environ.get("CHAT_CATALOG_DB_USER", "llmops")),
        )
    )
    password: str = field(
        default_factory=lambda: os.environ.get(
            "CHAT_MOLECULE_DB_PASSWORD",
            os.environ.get(
                "CHAT_QUERY_DB_PASSWORD",
                os.environ.get("CHAT_CATALOG_DB_PASSWORD", os.environ.get("CHAT_CACHE_DB_PASSWORD", "")),
            ),
        )
    )
    connect_timeout_s: int = field(default_factory=lambda: int(os.environ.get("CHAT_MOLECULE_DB_CONNECT_TIMEOUT_S", "3")))
    read_timeout_s: int = field(default_factory=lambda: int(os.environ.get("CHAT_MOLECULE_DB_READ_TIMEOUT_S", "15")))

    @staticmethod
    def molecule_sql() -> str:
        return """
            SELECT brand_key, brand_name, molecule_norm, molecule_display
            FROM mart_brand_molecule
            WHERE molecule_norm IS NOT NULL
              AND molecule_norm <> ''
            ORDER BY brand_name, brand_key, molecule_norm
        """

    def load(self) -> tuple[dict[str, str], ...]:
        import pymysql

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
                cursor.execute(self.molecule_sql())
                rows = cursor.fetchall()
        return tuple(
            {
                "brand_key": str(row.get("brand_key") or ""),
                "brand_name": str(row.get("brand_name") or ""),
                "molecule_norm": str(row.get("molecule_norm") or ""),
                "molecule_display": str(row.get("molecule_display") or row.get("molecule_norm") or ""),
            }
            for row in rows
            if row.get("molecule_norm")
        )


class TtlBrandMoleculeReader:
    def __init__(self, source: BrandMoleculeSource, ttl_seconds: int = 300, *, prewarm: bool = False) -> None:
        self._source = source
        self._ttl_seconds = ttl_seconds
        self._rows: tuple[dict[str, str], ...] | None = None
        self._loaded_at = 0.0
        self._lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._refreshing = False
        if prewarm:
            self.prewarm()

    def prewarm(self) -> None:
        threading.Thread(target=self._prewarm_snapshot, name="brand-molecule-prewarm", daemon=True).start()

    def _prewarm_snapshot(self) -> None:
        try:
            self._load_cold()
        except Exception:  # noqa: BLE001 - startup prewarm must not break request handling
            logger.exception("brand molecule snapshot prewarm failed")

    def brand_molecules(self) -> tuple[dict[str, str], ...]:
        with self._lock:
            rows = self._rows
            expired = rows is not None and time.monotonic() - self._loaded_at > self._ttl_seconds
            if expired and not self._refreshing:
                self._refreshing = True
                threading.Thread(target=self._refresh, name="brand-molecule-refresh", daemon=True).start()
            if rows is not None:
                return rows
        return self._load_cold()

    def _load_cold(self) -> tuple[dict[str, str], ...]:
        with self._load_lock:
            with self._lock:
                if self._rows is not None:
                    return self._rows
            rows = self._source.load()
            with self._lock:
                self._rows = rows
                self._loaded_at = time.monotonic()
                return rows

    def _refresh(self) -> None:
        try:
            rows = self._source.load()
        except Exception:  # noqa: BLE001 - preserve the last valid snapshot
            logger.exception("brand molecule TTL refresh failed")
            with self._lock:
                self._refreshing = False
            return
        with self._lock:
            self._rows = rows
            self._loaded_at = time.monotonic()
            self._refreshing = False


_SHARED_LOCK = threading.Lock()
_SHARED_READERS: dict[int, TtlBrandMoleculeReader] = {}


def shared_brand_molecule_reader(ttl_seconds: int = 300) -> TtlBrandMoleculeReader:
    with _SHARED_LOCK:
        reader = _SHARED_READERS.get(ttl_seconds)
        if reader is None:
            reader = TtlBrandMoleculeReader(MariaDbBrandMoleculeSource(), ttl_seconds=ttl_seconds, prewarm=True)
            _SHARED_READERS[ttl_seconds] = reader
        return reader
