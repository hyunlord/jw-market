"""Read-only mart probes: epoch fingerprint and new-brand detection.

Every query here is a SELECT; the orchestrator itself never writes to the
database. Builders own all writes.
"""

from __future__ import annotations

import os
from typing import Protocol

from pipeline.mart_config import resolve_mart_db_name


class Probe(Protocol):
    def current_epoch(self) -> str: ...

    def new_brand_keys(self, universe_sql: str, covered_sql: str) -> list[str]: ...


class MartProbe:
    """Live probe against the mart database (read-only)."""

    def __init__(self) -> None:
        self._connection = None

    def _connect(self):
        if self._connection is None:
            import pymysql

            self._connection = pymysql.connect(
                host=os.environ.get("MARIADB_HOST") or os.environ.get("DB_HOST", "127.0.0.1"),
                port=int(os.environ.get("MARIADB_PORT") or os.environ.get("DB_PORT", "3306")),
                user=os.environ.get("MARIADB_USER") or os.environ.get("DB_USER", "root"),
                password=os.environ.get("MARIADB_PASSWORD") or os.environ.get("DB_PASSWORD", ""),
                database=resolve_mart_db_name("MARIADB_DATABASE", "DB_NAME"),
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
            )
        return self._connection

    def current_epoch(self) -> str:
        from pipeline.scripts.etl.ops_forecast_store import mart_source_epoch

        return mart_source_epoch(self._connect())

    def new_brand_keys(self, universe_sql: str, covered_sql: str) -> list[str]:
        connection = self._connect()
        with connection.cursor() as cursor:
            cursor.execute(universe_sql)
            universe = {str(row["brand_key"]) for row in cursor.fetchall()}
            cursor.execute(covered_sql)
            covered = {str(row["brand_key"]) for row in cursor.fetchall()}
        return sorted(universe - covered)


class UnavailableProbe:
    """Fallback when the DB is unreachable: freshness is unknown.

    Dry-run planning proceeds with 'unknown' markers; execution fails closed.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def current_epoch(self) -> str:
        raise RuntimeError(f"mart probe unavailable: {self.reason}")

    def new_brand_keys(self, universe_sql: str, covered_sql: str) -> list[str]:
        raise RuntimeError(f"mart probe unavailable: {self.reason}")
