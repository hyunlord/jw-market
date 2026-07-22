from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
import threading
import time
from typing import Protocol

from .catalog_membership_paging import (
    GENERAL_MEMBERSHIP_PAGE_QUERY,
    CatalogMembershipCursor,
    load_general_membership_rows,
)


logger = logging.getLogger(__name__)

_SOURCE_PRIORITY = {
    "general_mart": 0,
    "catalog_alias": 1,
    "strategic_mart": 2,
}


class CatalogMembershipSource(Protocol):
    def load(self) -> tuple[dict[str, str], ...]: ...


@dataclass(slots=True)
class StaticCatalogMembershipReader:
    rows: tuple[dict[str, str], ...]
    calls: int = 0

    def load(self) -> tuple[dict[str, str], ...]:
        self.calls += 1
        return self.rows


@dataclass(frozen=True, slots=True)
class MariaDbCatalogMembershipReader:
    host: str = field(
        default_factory=lambda: os.environ.get(
            "CHAT_CATALOG_DB_HOST",
            os.environ.get("CHAT_QUERY_DB_HOST") or os.environ.get(
                "CHAT_CACHE_DB_HOST", "llmops-mariadb-service.llmops.svc.cluster.local"
            ),
        )
    )
    port: int = field(
        default_factory=lambda: int(
            os.environ.get(
                "CHAT_CATALOG_DB_PORT",
                os.environ.get("CHAT_QUERY_DB_PORT") or os.environ.get("CHAT_CACHE_DB_PORT", "3306"),
            )
        )
    )
    database: str = field(
        default_factory=lambda: os.environ.get(
            "CHAT_CATALOG_DB_NAME",
            os.environ.get("CHAT_QUERY_DB_NAME") or os.environ.get("CHAT_CACHE_DB_NAME", "jw_mart"),
        )
    )
    user: str = field(
        default_factory=lambda: os.environ.get(
            "CHAT_CATALOG_DB_USER",
            os.environ.get("CHAT_QUERY_DB_USER") or os.environ.get("CHAT_CACHE_DB_USER", "llmops"),
        )
    )
    password: str = field(
        default_factory=lambda: os.environ.get(
            "CHAT_CATALOG_DB_PASSWORD",
            os.environ.get("CHAT_QUERY_DB_PASSWORD") or os.environ.get("CHAT_CACHE_DB_PASSWORD", ""),
        )
    )
    connect_timeout_s: int = field(
        default_factory=lambda: int(os.environ.get("CHAT_CATALOG_DB_CONNECT_TIMEOUT_S", "3"))
    )
    read_timeout_s: int = field(
        default_factory=lambda: int(os.environ.get("CHAT_CATALOG_DB_READ_TIMEOUT_S", "15"))
    )
    general_page_size: int = field(
        default_factory=lambda: int(os.environ.get("CHAT_CATALOG_GENERAL_PAGE_SIZE", "2000"))
    )
    general_max_rows: int = field(
        default_factory=lambda: int(os.environ.get("CHAT_CATALOG_GENERAL_MAX_ROWS", "100000"))
    )

    @staticmethod
    def membership_queries() -> tuple[str, ...]:
        return (
            """
                SELECT DISTINCT
                       mart.brand_name AS brand,
                       NULL AS brand_alias,
                       mart.ml_id AS market_id,
                       COALESCE(market.name, mart.ml_id) AS market_name,
                       'strategic_mart' AS support_source
                FROM mart_strategic_ml_brand_metric AS mart
                LEFT JOIN catalog_ml_market AS market ON market.ml_id = mart.ml_id
                WHERE mart.brand_name IS NOT NULL
                  AND mart.brand_name <> ''
                  AND mart.ml_id IS NOT NULL
            """,
            """
                SELECT DISTINCT
                       COALESCE(
                           NULLIF(brand.general_brand_key, ''),
                           NULLIF(brand.canonical_name, ''),
                           NULLIF(brand.merge_name, ''),
                           brand.name
                       ) AS brand,
                       NULLIF(brand.name, '') AS brand_alias,
                       brand.ml_id AS market_id,
                       COALESCE(market.name, brand.ml_id) AS market_name,
                       'catalog_alias' AS support_source
                FROM catalog_strategic_brand AS brand
                INNER JOIN (
                    SELECT DISTINCT brand_id, ml_id
                    FROM mart_strategic_ml_brand_metric
                    WHERE brand_id IS NOT NULL
                      AND ml_id IS NOT NULL
                ) AS mart_brand
                    ON brand.brand_id = mart_brand.brand_id
                   AND brand.ml_id = mart_brand.ml_id
                LEFT JOIN catalog_ml_market AS market ON market.ml_id = brand.ml_id
                WHERE brand.is_excluded = 0
                  AND COALESCE(
                        NULLIF(brand.general_brand_key, ''),
                        NULLIF(brand.canonical_name, ''),
                        NULLIF(brand.merge_name, ''),
                        brand.name
                      ) IS NOT NULL
            """,
        )

    @staticmethod
    def general_membership_page_query() -> str:
        return GENERAL_MEMBERSHIP_PAGE_QUERY

    def load_general_membership_rows(
        self,
        cursor: CatalogMembershipCursor,
    ) -> tuple[dict[str, object], ...]:
        return load_general_membership_rows(
            cursor,
            page_size=self.general_page_size,
            max_rows=self.general_max_rows,
        )

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
                rows = []
                for query in self.membership_queries():
                    cursor.execute(query)
                    rows.extend(cursor.fetchall())
                rows.extend(self.load_general_membership_rows(cursor))
        return _merge_membership_rows(tuple(rows))


def _merge_membership_rows(rows: tuple[dict[str, object], ...]) -> tuple[dict[str, str], ...]:
    merged: dict[tuple[str, str, str, str], dict[str, str]] = {}
    for raw in rows:
        row = {
            "brand": str(raw["brand"]),
            "brand_alias": str(raw.get("brand_alias") or ""),
            "market_id": str(raw.get("market_id") or ""),
            "market_name": str(raw.get("market_name") or raw.get("market_id") or ""),
            "support_source": str(raw.get("support_source") or "general_mart"),
        }
        key = (row["brand"], row["brand_alias"], row["market_id"], row["market_name"])
        current = merged.get(key)
        if current is None or _SOURCE_PRIORITY.get(row["support_source"], -1) > _SOURCE_PRIORITY.get(
            current["support_source"], -1
        ):
            merged[key] = row
    return tuple(merged[key] for key in sorted(merged))


class TtlCatalogMembershipReader:
    def __init__(
        self,
        source: CatalogMembershipSource,
        ttl_seconds: int = 300,
        *,
        prewarm: bool = False,
    ) -> None:
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
        threading.Thread(
            target=self._prewarm_snapshot,
            name="catalog-membership-prewarm",
            daemon=True,
        ).start()

    def _prewarm_snapshot(self) -> None:
        try:
            self._load_cold()
        except Exception:  # noqa: BLE001 - startup prewarm must not break request handling
            logger.exception("catalog membership snapshot prewarm failed")

    def brand_memberships(self) -> tuple[dict[str, str], ...]:
        with self._lock:
            rows = self._rows
            expired = rows is not None and time.monotonic() - self._loaded_at > self._ttl_seconds
            if expired and not self._refreshing:
                self._refreshing = True
                threading.Thread(target=self._refresh, name="catalog-membership-refresh", daemon=True).start()
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
        except Exception:  # noqa: BLE001 - preserve the last valid catalog snapshot
            logger.exception("catalog membership TTL refresh failed")
            with self._lock:
                self._refreshing = False
            return
        with self._lock:
            self._rows = rows
            self._loaded_at = time.monotonic()
            self._refreshing = False


_SHARED_LOCK = threading.Lock()
_SHARED_READERS: dict[int, TtlCatalogMembershipReader] = {}


def shared_catalog_membership_reader(ttl_seconds: int = 300) -> TtlCatalogMembershipReader:
    with _SHARED_LOCK:
        reader = _SHARED_READERS.get(ttl_seconds)
        if reader is None:
            reader = TtlCatalogMembershipReader(
                MariaDbCatalogMembershipReader(),
                ttl_seconds=ttl_seconds,
                prewarm=True,
            )
            _SHARED_READERS[ttl_seconds] = reader
        return reader
